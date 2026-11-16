"""看图识别服务：Mock/replay 默认；可选真调用；缓存；事实与待确认入库。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
from PIL import Image
from dataclasses import dataclass, field
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from backend.evidence_selection import position_image_changed
from backend.local_reading import corroborated_prose
from backend.models import MockModelClient
from backend.models.errors import ModelError
from backend.models.openai_compat import CallResult
from backend.store import BBox, Evidence, Repository, Source, StoreError
from backend.title_identity import MANUAL_NUMBER_FIELD, has_manual_number
from backend.store.originals import sha256_file
from backend.store.paths import safe_path

from .bbox_util import clamp_bbox
from .cache import ExtractCache
from .errors import (
    CODE_BAD_FORMAT,
    CODE_INCOMPLETE,
    CODE_NO_IMAGE,
    ExtractError,
)
from .prompt import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_FORMAT_RETRIES,
    EXTRACT_ROLE,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from .replay import ReplayLookup
from .schema import parse_model_json, validate_extract_payload, normalize_numeric_fact
from .tiles import STRATEGY, MERGE_VERSION, plan_regions, map_fact, merge_facts, overlap_fraction, _located_text_bbox
from .positioned_notes import (
    POSITION_VERSION, cached_note_observations, has_reference_token,
    independently_reread_location, is_fastener_test_note, locate_note, locate_reference, locate_short_reference,
    normalized_text, reread_fastener_test_note, reread_note_lines, reread_note_omission, short_reference_token,
)

REFERENCE_LOCATION_REASON = "引用识读位置未核实"

logger = logging.getLogger("drawcheck.extract")


def _fact_anchor(row):
    return tuple(row.get(key) for key in ("unit_id", "text", "x0", "y0", "x1", "y1"))


def _stale_position_rows(rows, annotations, image_sha256):
    return {row["id"] for row in rows
            if position_image_changed(annotations.get(row["id"], {}), image_sha256)}


def can_retry_visual(root, unit):
    meta = unit.get("metadata") or {}
    if (meta.get("extract_status") != "incomplete" or meta.get("out_of_scope")
            or meta.get("review_scope") == "out" or meta.get("unreadable")):
        return False
    failure = meta.get("extract_model_failure")
    if failure is None:
        # Databases created before structured failures stored this exact prefix.
        code = (meta.get("extract_incomplete_reason") or "").partition("模型调用失败：")[2].partition(":")[0]
        transient = code in {"TIMEOUT", "RATE_LIMITED"} or (
            len(code) == len("OTHER_HTTP_500") and code.startswith("OTHER_HTTP_5") and code[-2:].isdigit())
    else:
        transient = isinstance(failure, dict) and failure.get("retryable") is True
    path = meta.get("review_relative_path") or meta.get("preview_relative_path")
    try:
        return bool(transient and path and safe_path(root, path).is_file())
    except (StoreError, ValueError, TypeError, OSError):
        return False


def _env_live() -> bool:
    return os.environ.get("DRAWCHECK_LIVE_MODEL") == "1"


def _env_use_mock() -> bool:
    """默认 Mock；仅当 DRAWCHECK_LIVE_MODEL=1 且未强制 mock 时走真模型。"""
    if os.environ.get("DRAWCHECK_EXTRACT_MOCK", "1") == "1" and not _env_live():
        return True
    if os.environ.get("DRAWCHECK_EXTRACT_MOCK") == "1":
        return True
    return not _env_live()


@dataclass
class ExtractResult:
    unit_id: str
    version_id: str
    status: str  # ok | incomplete
    prompt_version: str
    image_sha256: str | None = None
    cache_hit: bool = False
    model_calls: int = 0
    incomplete_reason: str | None = None
    cost_paused: bool = False
    title_blocks: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)
    pending_ids: list[str] = field(default_factory=list)
    bbox_clamp_notes: list[str] = field(default_factory=list)
    replay_match: str | None = None
    replay_warning: str | None = None
    role: str = EXTRACT_ROLE
    cache_identity: str | None = None
    catalog: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "version_id": self.version_id,
            "status": self.status,
            "prompt_version": self.prompt_version,
            "image_sha256": self.image_sha256,
            "cache_hit": self.cache_hit,
            "model_calls": self.model_calls,
            "incomplete_reason": self.incomplete_reason,
            "title_blocks": self.title_blocks,
            **self.catalog,
            "facts": self.facts,
            "fact_ids": self.fact_ids,
            "pending_ids": self.pending_ids,
            "bbox_clamp_notes": self.bbox_clamp_notes,
            "replay_match": self.replay_match,
            "replay_warning": self.replay_warning,
            "role": self.role,
        }


class ExtractService:
    """对单张/一批图纸单元执行看图识别（供 DC-10 挂接）。"""

    def __init__(
        self,
        store: Repository,
        *,
        prompt_version: str = PROMPT_VERSION,
        format_retries: int = DEFAULT_FORMAT_RETRIES,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        use_mock: bool | None = None,
        replay: ReplayLookup | None = None,
        model_client: Any | None = None,
        model_factory: Callable[[], Any] | None = None,
        cache: ExtractCache | None = None,
        task_id: str | None = None,
        provider: str = "mock",
        model_name: str = "mock-vl",
        model_base_url: str = "",
        model_credential_ref: str | None = None,
        allow_model_call: Callable[[], bool] | None = None,
        stream: bool = False,
    ):
        self.store = store
        self.prompt_version = prompt_version
        self.format_retries = int(format_retries)
        self.confidence_threshold = float(confidence_threshold)
        self.use_mock = _env_use_mock() if use_mock is None else use_mock
        self.replay = replay or ReplayLookup()
        self._model_client = model_client
        self._model_factory = model_factory
        self.cache = cache or ExtractCache(store.root)
        self.task_id = task_id
        self.provider = provider
        self.model_name = model_name
        self.allow_model_call = allow_model_call
        self.stream = stream
        self.region_progress_callback = None
        self._position_engine = None
        self.role = EXTRACT_ROLE
        # Isolate cached inference by actual model and execution mode.
        self.cache_role = f"{self.role}:{provider}:{model_name}:{model_base_url.rstrip('/')}:{'mock' if self.use_mock else 'live'}"
        if model_credential_ref is not None:
            route = json.dumps([provider, model_name, model_base_url, model_credential_ref])
            self.cache_role += ":route:" + hashlib.sha256(route.encode()).hexdigest()

    def _client(self) -> Any:
        if self._model_client is not None:
            return self._model_client
        if self._model_factory is not None:
            self._model_client = self._model_factory()
            return self._model_client
        if self.use_mock:
            self._model_client = MockModelClient(provider=self.provider, model=self.model_name)
            return self._model_client
        raise ExtractError(CODE_INCOMPLETE, "未配置模型客户端且非 Mock")

    def extract_unit(self, unit_id: str, *, persist: bool = True) -> ExtractResult:
        """Keep malformed cached/model evidence isolated to its drawing page."""
        try:
            return self._extract_unit(unit_id, persist=persist)
        except ExtractError as exc:
            unit = self.store.get("drawing_units", unit_id)
            result = ExtractResult(unit_id, unit["version_id"], "incomplete", self.prompt_version,
                incomplete_reason=exc.message, model_calls=getattr(exc, "model_calls", 0) or 0)
            if persist:
                self._mark_unit_incomplete(unit, result.incomplete_reason, exc.model_failure)
            return result

    def _cached(self, image_sha: str, role: str):
        try:
            cached = self.cache.get(image_sha, self.prompt_version, role)
        except (ValueError, TypeError, OSError) as exc:
            raise ExtractError(CODE_BAD_FORMAT, "识别缓存损坏或不可读取，该页未完成") from exc
        if cached is not None and (not isinstance(cached, dict) or "payload" not in cached):
            raise ExtractError(CODE_BAD_FORMAT, "识别缓存缺少有效 payload，该页未完成")
        return cached

    def _prepare_payload(self, payload):
        # Apply the same boundary to new inference, aggregate/region caches and
        # direct persistence. The schema retains region/conflict audit flags.
        payload = validate_extract_payload(payload)
        return self._clamp_payload(payload)

    def _extract_unit(self, unit_id: str, *, persist: bool = True) -> ExtractResult:
        unit = self.store.get("drawing_units", unit_id)
        version_id = unit["version_id"]
        image_path, image_sha, fallback_key, no_image_reason = self._resolve_review_image(unit)

        if image_path is None or image_sha is None:
            result = ExtractResult(
                unit_id=unit_id,
                version_id=version_id,
                status="incomplete",
                prompt_version=self.prompt_version,
                incomplete_reason=no_image_reason or "无审查图，跳过识别",
            )
            if persist:
                self._mark_unit_incomplete(unit, result.incomplete_reason)
            return result

        try:
            with Image.open(image_path) as image:
                image.verify()
                regions = plan_regions(*image.size) if not self.use_mock else []
        except (OSError, ValueError, Image.DecompressionBombError):
            result = ExtractResult(unit_id=unit_id, version_id=version_id, status="incomplete",
                prompt_version=self.prompt_version, incomplete_reason="审查图损坏或无法读取，识别未完成")
            if persist:
                self._mark_unit_incomplete(unit, result.incomplete_reason)
            return result
        cache_role = self.cache_role + (":" + STRATEGY if regions else "")
        cached = self._cached(image_sha, cache_role)
        # Region responses remain tied to the exact crop/model/prompt. A merger
        # upgrade must replay those responses. A location-only upgrade can
        # reuse the raw aggregate from the current merger, before localization.
        rebuild_aggregate = bool(regions and cached is not None and (
            cached.get("merge_version") != MERGE_VERSION or
            (cached.get("position_version") != POSITION_VERSION and "raw_payload" not in cached)))
        if rebuild_aggregate:
            cached = None
        if cached is not None:
            if self.region_progress_callback:
                count = len(regions) + 1 if regions else 1
                self.region_progress_callback(count, count)
            raw_payload = deepcopy(cached.get("raw_payload",cached["payload"]))
            result = self._result_from_payload(
                unit_id=unit_id,
                version_id=version_id,
                image_sha=image_sha,
                payload=raw_payload,
                cache_hit=True,
                model_calls=0,
                replay_match=cached.get("replay_match"),
                replay_warning=cached.get("replay_warning"),
            )
            result.cache_identity = hashlib.sha256(cache_role.encode()).hexdigest()
            self._position_result(result,unit,image_path)
            if persist and regions and cached.get("coverage"):
                metadata = dict(self.store.get("drawing_units", unit_id).get("metadata") or {})
                metadata["extract_coverage"] = cached["coverage"]
                self.store._update("drawing_units", unit_id, {"metadata_json": json.dumps(metadata, ensure_ascii=False)})
            copied = self._copied_recognition_is_persisted(unit, image_sha, cache_role)
            normalized_payload = {"title_blocks": result.title_blocks, "facts": result.facts, **result.catalog}
            changed_payload = cached["payload"] != normalized_payload
            metadata = unit.get("metadata") or {}
            if persist and (changed_payload or cached.get("position_version") != POSITION_VERSION or
                            metadata.get("extract_status") == "incomplete" or
                            metadata.get("extract_image_sha256") != image_sha or
                            (unit_id not in (cached.get("persisted_unit_ids") or []) and not copied)):
                self._persist(result, unit)
                cached["payload"] = normalized_payload
                cached["raw_payload"] = raw_payload
                cached["position_version"] = POSITION_VERSION
                cached["persisted"] = True
                cached["persisted_unit_ids"] = list(dict.fromkeys(list(cached.get("persisted_unit_ids") or []) + [unit_id]))
                cached["fact_ids"] = result.fact_ids
                cached["pending_ids"] = result.pending_ids
                self.cache.set(image_sha, self.prompt_version, cache_role, cached)
            elif persist:
                # 缓存已持久化过：仍需保证目录字段可被 catalog 使用；facts 不重复写入
                result.fact_ids = list(((unit.get("metadata") or {}).get("fact_annotations") or {}).keys())
                result.pending_ids = [p["id"] for p in self.store.list_records("pending_confirmations", version_id=version_id) if p["unit_id"] == unit_id]
            return result

        try:
            payload, meta = self._run_recognition(
                image_path=image_path,
                image_sha=image_sha,
                fallback_key=fallback_key,
                unit=unit,
                cache_only=rebuild_aggregate,
            )
        except ExtractError as exc:
            result = ExtractResult(
                unit_id=unit_id,
                version_id=version_id,
                status="incomplete",
                prompt_version=self.prompt_version,
                image_sha256=image_sha,
                model_calls=getattr(exc, "model_calls", 0) or 0,
                incomplete_reason=exc.message,
                cost_paused=getattr(exc, "cost_paused", False),
            )
            if persist:
                if not result.cost_paused:
                    self._mark_unit_incomplete(unit, result.incomplete_reason, exc.model_failure)
                if self.use_mock:
                    self._record_call(unit, image_sha, status="error", tokens=(0, 0), cost=0)
            return result

        result = self._result_from_payload(
            unit_id=unit_id,
            version_id=version_id,
            image_sha=image_sha,
            payload=payload,
            cache_hit=rebuild_aggregate,
            model_calls=meta.get("model_calls", 0),
            replay_match=meta.get("replay_match"),
            replay_warning=meta.get("replay_warning"),
            clamp_notes=meta.get("clamp_notes") or [],
        )
        result.cache_identity = hashlib.sha256(cache_role.encode()).hexdigest()
        raw_payload = deepcopy(payload)
        self._position_result(result,unit,image_path)
        if persist:
            self._persist(result, unit)
            if result.model_calls and self.use_mock:
                self._record_call(
                    unit,
                    image_sha,
                    status="ok",
                    tokens=meta.get("tokens") or (0, 0),
                    cost=meta.get("cost_micros") or 0,
                )
        cache_blob = {
            "raw_payload": raw_payload,
            "position_version": POSITION_VERSION,
            "payload": {
                "title_blocks": result.title_blocks,
                "facts": result.facts,
                **result.catalog,
            },
            "replay_match": result.replay_match,
            "replay_warning": result.replay_warning,
            "persisted": persist,
            "persisted_unit_ids": [unit_id] if persist else [],
            "fact_ids": result.fact_ids,
            "pending_ids": result.pending_ids,
            "coverage": (self.store.get("drawing_units", unit_id).get("metadata") or {}).get("extract_coverage"),
        }
        if regions:
            cache_blob["merge_version"] = MERGE_VERSION
        self.cache.set(image_sha, self.prompt_version, cache_role, cache_blob)
        return result

    def _position_result(self, result, unit, image_path):
        """Recover source text and its position using independent local observations."""
        from backend.references import drawing_reference, visible_reference_hint

        if self.use_mock:
            return

        def raw_anchor(category, text, bbox):
            return (category, text, *(bbox.get(k) for k in ("x0", "y0", "x1", "y1")))

        metadata = unit.get("metadata") or {}
        annotations = metadata.get("fact_annotations") or {}
        prior_anchors = {
            raw_anchor(annotation.get("category"), position.get("original_text"), annotation["model_original_bbox"])
            for annotation in annotations.values()
            if (position := annotation.get("position_recovery")) and position.get("image_sha256") == result.image_sha256
            and isinstance(annotation.get("model_original_bbox"), dict)
        }
        eligible = []
        for fact in result.facts:
            text = fact["text"]
            reference = drawing_reference(fact)
            unlocated = bool(fact.get("region_bbox_unlocated") or fact.get("bbox_clamped")
                             or not _located_text_bbox(fact["bbox"]))
            prose = (fact.get("numeric_value") is None
                     and fact.get("category") in {"说明", "施工说明", "施工要求", "设计要求", "材料规格", "其他"}
                     and len(normalized_text(text)) >= 12)
            # A detail caption and material grade can look like a reference.
            is_reference = bool(reference and (has_reference_token(text) or reference.get("target") or not prose))
            prior = raw_anchor(fact.get("category"), text, fact["bbox"]) in prior_anchors
            plain_note = prose and not is_reference and not visible_reference_hint(fact) and not any(
                fact.get(key) for key in (
                    "object_id", "comparison_key", "identity_confirmed", "unit", "quantity_role",
                    "target_drawing_number", "target_detail_number", "detail_number",
                    "numeric_value_invalid", "unit_invalid", "region_conflict"))
            fastener = plain_note and is_fastener_test_note(text)
            if is_reference or (prose and (unlocated or reference or prior or (fastener and self._should_pending(fact)))):
                eligible.append((fact, is_reference, plain_note, fastener, unlocated))
        if not eligible:
            return

        pending = [p for p in self.store.list_records("pending_confirmations", version_id=unit["version_id"])
                   if p["unit_id"] == unit["id"] and p["status"] in {"已确认", "无法确定"}]
        stored = [f for f in self.store.list_records("facts", version_id=unit["version_id"])
                  if f["unit_id"] == unit["id"] and f["source"] == Source.VISION.value]

        stale = _stale_position_rows(stored, annotations, result.image_sha256)
        stale_anchors = {_fact_anchor(row) for row in stored if row["id"] in stale}

        def box(row):
            return {k: row[k] for k in ("x0", "y0", "x1", "y1")}

        def protected(fact):
            for old in stored:
                if _fact_anchor(old) in stale_anchors:
                    continue
                annotation = annotations.get(old["id"], {})
                proof = annotation.get("position_recovery") or {}
                image_sha = proof.get("image_sha256") or metadata.get("extract_image_sha256")
                if image_sha and image_sha != result.image_sha256:
                    continue
                if "category" in annotation and annotation["category"] != fact.get("category"):
                    continue
                exact = old["text"] == fact["text"] and box(old) == fact["bbox"]
                recovered = (result.image_sha256 and proof.get("image_sha256") == result.image_sha256
                             and annotation.get("category") == fact.get("category")
                             and proof.get("original_text") == fact["text"]
                             and annotation.get("model_original_bbox") == fact["bbox"])
                if (exact or recovered) and any(p["text"] == old["text"] and box(p) == box(old) for p in pending):
                    return old, annotation
            return None

        candidates = []
        for entry in eligible:
            fact = entry[0]
            human = protected(fact)
            if not human:
                candidates.append(entry)
                continue
            old, annotation = human
            fact.update(text=old["text"], bbox=box(old))
            if annotation.get("position_recovery"):
                for key in ("position_recovery", "model_original_bbox", "model_original_flags",
                            "bbox_clamped", "region_bbox_unlocated", "unclear", "confidence"):
                    if key in annotation:
                        fact[key] = deepcopy(annotation[key])
                    else:
                        fact.pop(key, None)
        if not candidates:
            return

        def engine():
            if self._position_engine is None:
                from .local_sheet import _engine
                self._position_engine = _engine()
            return self._position_engine

        ocr_error = None
        try:
            bundle = cached_note_observations(image_path, cache_root=self.store.root, engine_factory=engine)
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            bundle, ocr_error = None, type(exc).__name__

        def match(fact, is_reference, observations):
            if not observations:
                return {"status": "invalid", "locations": [], "reason": "local_ocr_unavailable", "error": ocr_error}
            locate = locate_reference if is_reference else locate_note
            return locate(fact["text"], observations, image_sha256=result.image_sha256)

        initial = [match(fact, ref, bundle) for fact, ref, _, _, _ in candidates]
        queries = [fact["text"] for (fact, _, _, _, _), located in zip(candidates, initial)
                   if located["status"] == "missing"]
        expanded = bundle
        if bundle and queries:
            try:
                expanded = reread_note_lines(image_path, bundle, queries, engine=engine)
            except (ImportError, OSError, ValueError, RuntimeError) as exc:
                logger.info("本地原图文字复读不可用：%s", type(exc).__name__)
        positioned = {"unique_exact", "unique_format_equivalent", "unique_reread"}
        for (fact, is_reference, plain_note, fastener, unlocated), located in zip(candidates, initial):
            original_text = fact["text"]
            original_bbox = deepcopy(fact["bbox"])
            original_flags = {key: deepcopy(fact[key]) for key in (
                "unclear", "confidence", "bbox_clamped", "region_bbox_unlocated", "region_ids", "region_conflict")
                if key in fact}
            if expanded is not bundle and located["status"] != "invalid":
                retried = match(fact, is_reference, expanded)
                if retried["status"] == "ambiguous":
                    located = retried
                elif located["status"] == "missing" and independently_reread_location(retried, expanded, reference=is_reference):
                    located = {**retried, "recovery_stage": "corroborated_native_line_reread"}

            scope = "drawing_reference" if is_reference else "note"
            if bundle and located["status"] not in positioned | {"ambiguous"}:
                try:
                    if is_reference and short_reference_token(original_text) and not fact.get("region_conflict"):
                        located = locate_short_reference(original_text, bundle, image_sha256=result.image_sha256,
                                                         image_path=image_path, engine=engine)
                    elif fastener and located["status"] == "missing":
                        scope = "fastener_test_note"
                        reading = reread_fastener_test_note(original_text, image_path, bundle,
                                                            image_sha256=result.image_sha256, engine=engine)
                        if not visible_reference_hint({**fact, "text": reading.get("reread_text", original_text)}):
                            located = reading
                    elif (plain_note and located["status"] == "missing"
                          and fact.get("category") in {"说明", "施工说明", "施工要求", "设计要求"}):
                        reading = reread_note_omission(image_path, bundle, original_text, engine=engine)
                        if reading and not visible_reference_hint({**fact, "text": reading["transcribed_text"]}):
                            located = reading
                except (ImportError, OSError, ValueError, RuntimeError) as exc:
                    logger.info("本地原图专项复读不可用：%s", type(exc).__name__)

            reading_status = "unchanged"
            if located["status"] in positioned and len(located["locations"]) == 1:
                location, = located["locations"]
                fact["bbox"] = deepcopy(location["bbox"])
                fact.pop("bbox_clamped", None)
                fact.pop("region_bbox_unlocated", None)
                if located["status"] == "unique_reread":
                    reading_status = "independent_reread"
                    fact.update(text=located["reread_text"], unclear=False, confidence=location["confidence"])
                elif "transcribed_text" in located:
                    reading_status = "locally_transcribed"
                    fact["text"] = located["transcribed_text"]
                elif "recovered_text" in located and located["recovered_text"] != original_text:
                    reading_status = "locally_reread"
                    fact["text"] = located["recovered_text"]
            elif unlocated or not fastener:
                fact["region_bbox_unlocated"] = True

            fact.update(model_original_bbox=original_bbox, model_original_flags=original_flags,
                        position_recovery={**located, "version": POSITION_VERSION, "scope": scope,
                                           "image_sha256": result.image_sha256, "original_text": original_text,
                                           "original_unclear": bool(original_flags.get("unclear")),
                                           "reading_status": reading_status})

    def extract_version(self, version_id: str, *, persist: bool = True) -> list[ExtractResult]:
        units = self.store.list_records("drawing_units", version_id=version_id)
        return [self.extract_unit(u["id"], persist=persist) for u in units]

    # --- internals ---

    def _resolve_review_image(
        self, unit: dict
    ) -> tuple[Path | None, str | None, str | None, str | None]:
        meta = unit.get("metadata") or {}
        rel = meta.get("review_relative_path") or meta.get("preview_relative_path")
        if not rel:
            return None, None, None, "组件不可用或无审查图，跳过识别"
        try:
            path = safe_path(self.store.root, rel)
        except Exception:
            return None, None, None, "审查图路径非法"
        if not path.is_file():
            return None, None, None, "审查图文件不存在"
        digest = sha256_file(path)
        # 备用键：原图 sha + 页序（ordinal）
        fallback = None
        try:
            original = self.store.get("original_files", unit["original_id"])
            fallback = f"{original['sha256']}:{unit['ordinal']}"
        except Exception:
            fallback = None
        return path, digest, fallback, None

    def _copied_recognition_is_persisted(self, unit, image_sha, cache_role):
        metadata = unit.get("metadata") or {}
        if not metadata.get("copied_from_unit_id") or metadata.get("extract_status") != "ok":
            return False
        if metadata.get("extract_image_sha256") != image_sha or metadata.get("extract_prompt_version") != self.prompt_version:
            return False
        if metadata.get("extract_cache_identity") != hashlib.sha256(cache_role.encode()).hexdigest():
            return False
        if metadata.get("extract_position_version") != POSITION_VERSION:
            return False
        if cache_role.endswith(STRATEGY):
            if metadata.get("extract_strategy") != STRATEGY or metadata.get("extract_merge_version") != MERGE_VERSION:
                return False
        ids = set((metadata.get("fact_annotations") or {}).keys())
        actual = {row["id"] for row in self.store.list_records("facts", version_id=unit["version_id"]) if row["unit_id"] == unit["id"]}
        return ids <= actual

    def _run_recognition(self, *, image_path, image_sha, fallback_key, unit=None, cache_only=False):
        with Image.open(image_path) as source:
            size = source.size
            regions = plan_regions(*size) if not self.use_mock else []
            if not regions:
                if self.region_progress_callback:
                    self.region_progress_callback(0, 1)
                result = self._run_single_recognition(image_path=image_path, image_sha=image_sha,
                    fallback_key=fallback_key, unit=unit)
                if self.region_progress_callback:
                    self.region_progress_callback(1, 1)
                return result
            source.load()
            total = len(regions) + 1
            completed, calls, titles, facts, local_titles = 0, 0, [], [], []
            catalogs = []
            coverage = {"strategy": STRATEGY, "total_regions": total, "completed_regions": 0,
                        "regions": [{"id": "overview", "box": [0, 0, *size]}] +
                                   [{"id": f"region-{i+1}", "box": list(box)} for i, box in enumerate(regions)]}
            def checkpoint():
                coverage["completed_regions"] = completed
                if unit:
                    metadata = dict(self.store.get("drawing_units", unit["id"]).get("metadata") or {})
                    metadata["extract_coverage"] = coverage
                    self.store._update("drawing_units", unit["id"], {"metadata_json": json.dumps(metadata, ensure_ascii=False)})
                if self.region_progress_callback:
                    self.region_progress_callback(completed, total)
            checkpoint()
            with tempfile.TemporaryDirectory(prefix="drawcheck-regions-") as temporary:
                for index, region in enumerate(coverage["regions"]):
                    overview = index == 0
                    path = image_path
                    if not overview:
                        path = Path(temporary) / f"region-{index}.png"
                        source.crop(tuple(region["box"])).save(path)
                    digest = sha256_file(path)
                    role = f"{self.cache_role}:{STRATEGY}:{image_sha}:{region['id']}:{region['box']}"
                    cached = None
                    instruction = ("本次是整张图纸概览：提取图签、目录表和整体身份，facts返回空数组。若是目录页，逐行读取完整图号列；看不清或未读完整必须catalog_complete=false，不得推算图号。不要推断缩小后看不清的细节。" if overview else
                        "本次输入是图纸的局部裁剪区域。bbox必须是当前输入图片内的0~1归一化坐标，不是整张图坐标。"
                        "仅在局部可见完整图签时提供title_blocks，不能凭空推断整图身份。局部可见目录表时提供确实读到的图号及局部坐标；只有完整目录表全部边界和行都在当前裁剪内且读清时catalog_complete=true，否则false。只凭当前区域内明确编号、轴线和位置确认对象；缺乏身份依据时identity_confirmed=false。")
                    try:
                        cached = self._cached(digest, role)
                        if cached:
                            payload, _ = self._prepare_payload(cached["payload"])
                        else:
                            if cache_only:
                                raise ExtractError(CODE_INCOMPLETE,
                                    "分块合并规则已更新，但原区域识别缓存缺失；未自动发起新的模型请求，需补充该区域识别")
                            payload, meta = self._run_single_recognition(image_path=path, image_sha=digest,
                                fallback_key=None, unit=unit, instruction=instruction)
                            calls += meta.get("model_calls", 0)
                            self.cache.set(digest, self.prompt_version, role, {"payload": payload})
                    except ExtractError as exc:
                        region["state"] = "failed"
                        coverage["incomplete_region_ids"] = [r["id"] for r in coverage["regions"] if r.get("state") != "completed"]
                        checkpoint()
                        error = ExtractError(CODE_INCOMPLETE, f"大图区域识别未完成（{completed}/{total}）：{exc.message}")
                        error.model_calls = calls + (getattr(exc, "model_calls", 0) or 0)
                        error.cost_paused = getattr(exc, "cost_paused", False)
                        error.model_failure = exc.model_failure
                        raise error from exc
                    if "is_catalog_sheet" in payload:
                        catalog = {key:value for key,value in payload.items() if key.startswith("catalog_") or key == "is_catalog_sheet"}
                        if not overview:
                            if catalog.get("catalog_bbox"):
                                catalog["catalog_bbox"] = map_fact({"bbox":catalog["catalog_bbox"]}, region["box"], size, region["id"])["bbox"]
                            catalog["catalog_entries"] = [map_fact(entry, region["box"], size, region["id"]) for entry in catalog.get("catalog_entries", [])]
                        catalogs.append(catalog)
                    if overview:
                        titles = payload["title_blocks"]
                    else:
                        facts.extend(map_fact(fact, region["box"], size, region["id"]) for fact in payload["facts"])
                        local_titles.extend(map_fact(block, region["box"], size, region["id"]) for block in payload["title_blocks"])
                    region["state"] = "completed"
                    region["cache_hit"] = bool(cached)
                    completed += 1
                    checkpoint()
            catalog_result = self._merge_catalogs(catalogs)
            for block in titles + local_titles:
                if not block.get("drawing_number", "").strip():
                    block["unclear"] = True
            def reliable(block):
                return bool(block.get("drawing_number", "").strip()) and not self._should_pending(block) and not block.get("no_title_block")
            if not any(reliable(block) for block in titles):
                candidates = [block for block in local_titles if reliable(block)]
                # Keep uncertain overview evidence in its region cache; use clearly
                # readable local title blocks for the actual directory when available.
                if candidates or not titles:
                    titles = []
                for block in (candidates or local_titles if not titles else []):
                    if not any(block.get("drawing_number") == existing.get("drawing_number") and
                               overlap_fraction(block["bbox"], existing["bbox"]) >= .7 for existing in titles):
                        titles.append(block)
                if (not titles or not any(block.get("drawing_number", "").strip() or block.get("name", "").strip() for block in titles)) and not catalog_result.get("is_catalog_sheet"):
                    error = ExtractError(CODE_INCOMPLETE, "大图概览及局部区域均未识别到图签，图纸身份需补充确认")
                    error.model_calls = calls
                    raise error
            return {"title_blocks": titles, "facts": merge_facts(facts), **catalog_result}, {"model_calls": calls}

    def _merge_catalogs(self, catalogs):
        if not catalogs:
            return {}
        actual = [catalog for catalog in catalogs if catalog.get("is_catalog_sheet") is True]
        if not actual:
            return {"is_catalog_sheet":False}
        volumes = set(number for catalog in actual for number in catalog.get("catalog_volume_numbers", []))
        numbers, entries, candidates = [], [], []
        for catalog in actual:
            uncertain = {entry.get("drawing_number") for entry in catalog.get("catalog_entries", []) if self._should_pending(entry)}
            listed = list(dict.fromkeys(number for number in catalog.get("catalog_listed_numbers", []) if number not in volumes and number not in uncertain))
            readable = [entry for entry in catalog.get("catalog_entries", []) if entry.get("drawing_number") not in volumes and entry.get("drawing_number") and not self._should_pending(entry)]
            for number in listed + [entry["drawing_number"] for entry in readable]:
                if number not in numbers:
                    numbers.append(number)
            entries.extend(readable)
            if catalog.get("catalog_complete") is True and listed and all(not self._should_pending(entry) for entry in catalog.get("catalog_entries", [])):
                candidates.append(set(listed))
        # Partial table crops never become complete merely by union or successful API calls.
        complete = bool(candidates) and all(candidate == set(numbers) for candidate in candidates)
        result = {"is_catalog_sheet":True, "catalog_listed_numbers":numbers, "catalog_volume_numbers":sorted(volumes),
            "catalog_entries":entries, "catalog_complete":complete}
        if any(catalog.get("catalog_volume_numbers_uncertain") for catalog in actual):
            result["catalog_volume_numbers_uncertain"] = True
        boxes = [catalog["catalog_bbox"] for catalog in actual if catalog.get("catalog_bbox")]
        if boxes:
            result["catalog_bbox"] = {"x0":min(box["x0"] for box in boxes), "y0":min(box["y0"] for box in boxes),
                "x1":max(box["x1"] for box in boxes), "y1":max(box["y1"] for box in boxes)}
        if not complete:
            result["catalog_incomplete_reason"] = "目录表未完整读清，或不同区域读取的图号不一致"
        return result

    def _run_single_recognition(
        self, *, image_path: Path, image_sha: str, fallback_key: str | None, unit: dict | None = None, instruction: str = ""
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        meta: dict[str, Any] = {"model_calls": 0, "tokens": (0, 0), "cost_micros": 0}
        if self.use_mock:
            entry, kind, warning = self.replay.lookup(
                review_png_sha256=image_sha, fallback_key=fallback_key
            )
            meta["replay_match"] = kind
            meta["replay_warning"] = warning
            if entry is None:
                # Mock 无命中：仍计一次「逻辑调用」但不外连；标未完成
                meta["model_calls"] = 1
                raise ExtractError(CODE_INCOMPLETE, "Mock 回放未命中审查图 SHA 与备用键")
            if kind == "fallback" and warning:
                # 测试不得静默当成功：带 warning，仍返回数据
                logger.warning(warning)
            raw = self.replay.build_mock_payload(entry)
            # 走一遍 Mock 客户端以保持调用计数可测（可选）
            client = self._client()
            if isinstance(client, MockModelClient):
                # 不把结果当权威，只记调用；内容以 replay 为准
                try:
                    client.chat_completions(
                        model=self.model_name,
                        messages=[{"role": "user", "content": f"replay:{image_sha[:12]}"}],
                    )
                    meta["model_calls"] = 1
                    meta["tokens"] = (12, 8)
                except ModelError:
                    meta["model_calls"] = 1
            payload = validate_extract_payload(raw)
            payload, clamp_notes = self._clamp_payload(payload)
            meta["clamp_notes"] = clamp_notes
            return payload, meta

        # 真调用路径
        data_url = _png_data_url(image_path)
        client = self._client()
        attempts = self.format_retries + 1
        last_err: Exception | None = None
        for attempt in range(attempts):
            if self.allow_model_call is not None and not self.allow_model_call():
                err = ExtractError(CODE_INCOMPLETE, "费用上限，审查已暂停")
                err.model_calls = meta["model_calls"]
                err.cost_paused = True
                raise err
            response = None
            meta["model_calls"] = meta.get("model_calls", 0) + 1
            user_prompt = build_user_prompt(self.prompt_version) + "\n" + instruction
            if attempt > 0 and isinstance(last_err, ExtractError):
                # Send only a bounded validator error, never provider reasoning
                # or the previous response. Preserve the first request exactly.
                summary = " ".join(last_err.message.split())[:240]
                user_prompt += (f"\n上次结构化输出未通过校验：{summary}。请重新看图并重新返回完整结果，保持原有审查覆盖范围。"
                    "每条事实必须提供图上可见的原文 text，不能凭 numeric_value 或 unit 拼造原文；"
                    "不可辨认时请标 unclear=true，不得猜测，也不能省略 text。")
            try:
                result: CallResult = client.chat_completions(
                    model=self.model_name,
                    max_tokens=8192,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT + "\n" + instruction},
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": data_url}},
                                {"type": "text", "text": user_prompt},
                            ],
                        },
                    ],
                    **({"stream": True} if self.stream else {}),
                )
                response = result
                diagnostics = (getattr(result, "extras", None) or {}).get("request", {})
                meta["tokens"] = (result.input_tokens, result.output_tokens)
                from backend.models.cost import estimate_cost_micros
                from backend.models.presets import price_for_model
                price = price_for_model(self.provider, self.model_name)
                input_tokens, output_tokens = meta["tokens"]
                if self.stream and (input_tokens is None or output_tokens is None):
                    # Preserve unknown actual usage. Use a conservative estimate for the local fee gate.
                    if input_tokens is None:
                        input_tokens = len(data_url) + len((SYSTEM_PROMPT + user_prompt).encode()) + 256
                    if output_tokens is None:
                        output_tokens = 8192
                estimate = estimate_cost_micros(input_tokens=input_tokens, output_tokens=output_tokens,
                    input_price_per_m_yuan=price.input_per_m_yuan, output_price_per_m_yuan=price.output_per_m_yuan)
                call_cost = int(estimate.estimated_cost_micros or 0)
                meta["cost_micros"] += call_cost
                if getattr(result, "finish_reason", None) == "length":
                    # A syntactically complete prefix is not proof that all evidence was emitted.
                    if unit is not None:
                        self._record_call(unit, image_sha, status="truncated", tokens=meta["tokens"], cost=call_cost,
                                          diagnostics=diagnostics)
                    error = ExtractError(CODE_INCOMPLETE, "模型输出达到长度上限，识别内容可能不完整，未作为完整结果保存")
                    error.model_calls = meta["model_calls"]
                    error.truncated = True
                    raise error
                parsed = parse_model_json(result.content)
                payload = validate_extract_payload(parsed)
                payload, clamp_notes = self._clamp_payload(payload)
                meta["clamp_notes"] = clamp_notes
                if unit is not None:
                    self._record_call(unit, image_sha, status="ok", tokens=meta["tokens"], cost=call_cost,
                                      diagnostics=diagnostics)
                return payload, meta
            except ExtractError as exc:
                if getattr(exc, "truncated", False):
                    raise
                if unit is not None and response is not None:
                    self._record_call(unit, image_sha, status="bad_format", tokens=meta["tokens"], cost=call_cost,
                                      diagnostics=diagnostics)
                last_err = exc
                if exc.code != CODE_BAD_FORMAT or attempt + 1 >= attempts:
                    err = ExtractError(CODE_INCOMPLETE, f"识别格式校验失败：{exc.message}")
                    err.model_calls = meta["model_calls"]  # type: ignore[attr-defined]
                    raise err from exc
            except ModelError as exc:
                if unit is not None:
                    self._record_call(unit, image_sha, status="error", tokens=(0, 0), cost=0,
                                      diagnostics=exc.diagnostics)
                err = ExtractError(CODE_INCOMPLETE, f"模型调用失败：{exc}")
                err.model_calls = meta["model_calls"]  # type: ignore[attr-defined]
                err.model_failure = {"code": exc.code, "retryable": exc.retryable}
                raise err from exc
        err = ExtractError(CODE_INCOMPLETE, f"识别失败：{last_err}")
        err.model_calls = meta["model_calls"]  # type: ignore[attr-defined]
        raise err

    def _clamp_payload(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        notes: list[str] = []
        for block in payload["title_blocks"]:
            bbox, clamped, n = clamp_bbox(block.get("bbox"))
            block["bbox"] = bbox
            if clamped:
                block["bbox_clamped"] = True
                block["unclear"] = True
                notes.extend(n)
        if payload.get("catalog_bbox") is not None:
            payload["catalog_bbox"], _, extra = clamp_bbox(payload["catalog_bbox"])
            notes.extend(extra)
        for entry in payload.get("catalog_entries", []):
            entry["bbox"], clamped, extra = clamp_bbox(entry.get("bbox"))
            if clamped:
                entry["unclear"] = True
                payload["catalog_complete"] = False
            notes.extend(extra)
        for fact in payload["facts"]:
            bbox, clamped, n = clamp_bbox(fact.get("bbox"))
            fact["bbox"] = bbox
            if clamped:
                fact["bbox_clamped"] = True
                fact["unclear"] = True
                notes.extend(n)
        return payload, notes

    def _result_from_payload(
        self,
        *,
        unit_id: str,
        version_id: str,
        image_sha: str,
        payload: dict[str, Any],
        cache_hit: bool,
        model_calls: int,
        replay_match: str | None = None,
        replay_warning: str | None = None,
        clamp_notes: list[str] | None = None,
    ) -> ExtractResult:
        payload, boundary_notes = self._prepare_payload(payload)
        return ExtractResult(
            unit_id=unit_id,
            version_id=version_id,
            status="ok",
            prompt_version=self.prompt_version,
            image_sha256=image_sha,
            cache_hit=cache_hit,
            model_calls=model_calls,
            title_blocks=list(payload.get("title_blocks") or []),
            facts=[normalize_numeric_fact(fact) for fact in payload.get("facts") or []],
            catalog=self._merge_catalogs([{key:value for key,value in payload.items() if key.startswith("catalog_") or key == "is_catalog_sheet"}]) if "is_catalog_sheet" in payload else {},
            bbox_clamp_notes=list(clamp_notes or []) + boundary_notes,
            replay_match=replay_match,
            replay_warning=replay_warning,
        )

    def _persist(self, result: ExtractResult, unit: dict) -> None:
        payload, notes = self._prepare_payload({"title_blocks": result.title_blocks, "facts": result.facts, **result.catalog})
        result.title_blocks, result.facts = payload["title_blocks"], payload["facts"]
        result.catalog = self._merge_catalogs([{key:value for key,value in payload.items() if key.startswith("catalog_") or key == "is_catalog_sheet"}]) if "is_catalog_sheet" in payload else {}
        result.bbox_clamp_notes.extend(notes)
        # Replace prior machine extraction atomically when recognition strategy changes.
        # Keep human-confirmed anchors and user facts, so their audit and override survive.
        with self.store._transaction():
            metadata = dict(self.store.get("drawing_units", unit["id"]).get("metadata") or {})
            annotations = dict(metadata.get("fact_annotations") or {})
            pending = [p for p in self.store.list_records("pending_confirmations", version_id=unit["version_id"]) if p["unit_id"] == unit["id"]]
            stored = [fact for fact in self.store.list_records("facts", version_id=unit["version_id"])
                      if fact["unit_id"] == unit["id"] and fact["id"] in annotations
                      and fact["source"] == Source.VISION.value]
            stale = _stale_position_rows(stored, annotations, result.image_sha256)
            stale_anchors = {_fact_anchor(row) for row in stored if row["id"] in stale}
            for fact in stored:
                anchor = _fact_anchor(fact)
                matches = [p for p in pending if _fact_anchor(p) == anchor]
                if (any(p["status"] in {"已确认", "无法确定"} for p in matches)
                        and (fact["id"] in stale or anchor not in stale_anchors)):
                    continue
                self.store._connection.execute("DELETE FROM facts WHERE id=?", (fact["id"],))
                annotations.pop(fact["id"], None)
                for p in matches:
                    if p["status"] == "待确认" and (p["reason"] in {"看不清", "把握度低于阈值", "分块读数不一致", REFERENCE_LOCATION_REASON}
                            or any(f.get("position_recovery",{}).get("scope")=="drawing_reference"
                                   and f["position_recovery"].get("original_text")==fact["text"]
                                   and f.get("model_original_bbox")=={k:fact[k] for k in ("x0","y0","x1","y1")}
                                   for f in result.facts)):
                        self.store._connection.execute("DELETE FROM pending_confirmations WHERE id=?", (p["id"],))
            metadata["fact_annotations"] = annotations
            self.store._update("drawing_units", unit["id"], {"metadata_json": json.dumps(metadata, ensure_ascii=False)})
            self._persist_rows(result, self.store.get("drawing_units", unit["id"]))

    def _pending_once(self, version_id, evidence, reason):
        columns = evidence.columns()
        for row in self.store.list_records("pending_confirmations", version_id=version_id):
            if all(row.get(key) == value for key, value in columns.items()):
                return row
        return self.store.create_pending(version_id, evidence, reason)

    def _persist_rows(self, result: ExtractResult, unit: dict) -> None:
        version_id = result.version_id
        annotations = dict((unit.get("metadata") or {}).get("fact_annotations") or {})
        def fact_key(row, annotation):
            return (tuple(row.get(key) for key in ("unit_id", "source", "text", "x0", "y0", "x1", "y1", "numeric_value", "unit"))
                    + tuple(annotation.get(key) for key in ("category", "object_id", "comparison_key",
                            "target_drawing_number", "target_detail_number", "detail_number", "quantity_role"))
                    + (annotation.get("identity_confirmed") is True,))
        stored = self.store.list_records("facts", version_id=version_id)
        stale = _stale_position_rows(stored, annotations, result.image_sha256)
        stale_anchors = {_fact_anchor(row) for row in stored if row["id"] in stale}
        human_anchors = {_fact_anchor(row) for row in self.store.list_records("pending_confirmations", version_id=version_id)
                         if row["status"] in {"已确认", "无法确定"}}
        protected = {row["id"] for row in stored if _fact_anchor(row) in human_anchors - stale_anchors}
        existing_facts = {fact_key(row, annotations.get(row["id"], {})): row
                          for row in stored if row["id"] not in stale}
        for index, fact in enumerate(result.facts):
            fact = normalize_numeric_fact(fact)
            result.facts[index] = fact
            bbox = BBox(**fact["bbox"])
            evidence = Evidence(result.unit_id, Source.VISION, fact["text"], bbox)
            numeric = fact.get("numeric_value")
            if numeric is not None and not isinstance(numeric, str):
                numeric = str(numeric)
            key = fact_key({**evidence.columns(), "numeric_value": numeric, "unit": fact.get("unit")}, fact)
            row = existing_facts.get(key)
            row = row or self.store.add_fact(
                version_id,
                evidence,
                numeric_value=numeric,
                unit=fact.get("unit"),
            )
            existing_facts[key] = row
            result.fact_ids.append(row["id"])
            reading_question = self._should_pending(fact)
            transcribed = (fact.get("position_recovery") or {}).get("reading_status") == "locally_transcribed"
            audit_only = bool((reading_question or transcribed) and row["id"] not in protected and result.image_sha256
                              and corroborated_prose(fact, bbox=fact["bbox"]) == result.image_sha256)
            if row["id"] not in protected:
                annotations[row["id"]] = {key: fact[key] for key in ("category", "object_id", "comparison_key", "identity_confirmed", "target_drawing_number", "target_detail_number", "detail_number", "quantity_role", "confidence", "unclear", "region_ids", "region_conflict", "numeric_value_invalid", "unit_invalid", "bbox_clamped", "region_bbox_unlocated", "model_original_bbox", "model_original_flags", "position_recovery") if key in fact}
                if audit_only:
                    annotations[row["id"]]["positioned_reading_only"] = True
            if row["id"] not in protected and fact.get("region_bbox_unlocated") and fact.get("position_recovery",{}).get("scope")=="drawing_reference":
                pending=self._pending_once(version_id,evidence,REFERENCE_LOCATION_REASON)
                result.pending_ids.append(pending["id"])
            if reading_question and not audit_only:
                reason = "分块读数不一致" if fact.get("region_conflict") else "看不清" if fact.get("unclear") else "把握度低于阈值"
                pending = self._pending_once(version_id, evidence, reason)
                result.pending_ids.append(pending["id"])

        manual_title = has_manual_number(unit)
        for block in result.title_blocks:
            if not manual_title and self._should_pending(block):
                bbox = BBox(**block["bbox"])
                text = block.get("drawing_number") or block.get("name") or "图签"
                evidence = Evidence(result.unit_id, Source.VISION, text, bbox)
                reason = "看不清" if block.get("unclear") else "把握度低于阈值"
                pending = self._pending_once(version_id, evidence, reason)
                result.pending_ids.append(pending["id"])

        # 写入提取状态到 metadata（不改表结构）
        meta = dict(self.store.get("drawing_units", unit["id"]).get("metadata") or {})
        if meta.get("manual_correction") and not manual_title and MANUAL_NUMBER_FIELD not in meta:
            # Preserve the old uncertainty before a new visual reading changes
            # it; a legacy name edit must not become number confirmation later.
            meta[MANUAL_NUMBER_FIELD] = False
        primary = result.title_blocks[0] if result.title_blocks else {}
        meta["title_identity_uncertain"] = (not bool((unit.get("drawing_number") or "").strip()) if manual_title else
            not primary.get("drawing_number", "").strip() or bool(primary.get("no_title_block")) or self._should_pending(primary))
        if manual_title:
            meta.update(title_drawing_number=unit["drawing_number"], no_title_block=False)
        if result.title_blocks:
            meta["out_of_scope"] = bool(result.title_blocks[0].get("out_of_scope"))
            if not manual_title:
                meta["title_drawing_number"] = result.title_blocks[0].get("drawing_number") or ""
            meta["title_bbox"] = result.title_blocks[0].get("bbox")
        if result.catalog:
            meta.update(result.catalog)
        meta["fact_annotations"] = annotations
        meta["extract_status"] = result.status
        meta["extract_prompt_version"] = result.prompt_version
        meta["extract_image_sha256"] = result.image_sha256
        meta["extract_cache_identity"] = result.cache_identity
        meta["extract_position_version"] = POSITION_VERSION
        meta["extract_strategy"] = STRATEGY if meta.get("extract_coverage", {}).get("strategy") == STRATEGY else "whole"
        if meta["extract_strategy"] == STRATEGY:
            meta["extract_merge_version"] = MERGE_VERSION
        else:
            meta.pop("extract_merge_version", None)
        meta.pop("extract_incomplete_reason", None)
        meta.pop("extract_model_failure", None)
        if result.incomplete_reason:
            meta["extract_incomplete_reason"] = result.incomplete_reason
        self.store._update(
            "drawing_units",
            unit["id"],
            {"metadata_json": json.dumps(meta, ensure_ascii=False)},
        )

    def _should_pending(self, item: dict[str, Any]) -> bool:
        if item.get("unclear"):
            return True
        conf = item.get("confidence")
        if conf is not None and float(conf) < self.confidence_threshold:
            return True
        return False

    def _mark_unit_incomplete(self, unit: dict, reason: str | None, model_failure: dict | None = None) -> None:
        meta = dict(self.store.get("drawing_units", unit["id"]).get("metadata") or {})
        meta["extract_status"] = "incomplete"
        meta["extract_incomplete_reason"] = reason or "未完成"
        if model_failure is None:
            meta.pop("extract_model_failure", None)
        else:
            meta["extract_model_failure"] = model_failure
        self.store._update(
            "drawing_units",
            unit["id"],
            {"metadata_json": json.dumps(meta, ensure_ascii=False)},
        )

    def _record_call(
        self,
        unit: dict,
        image_sha: str,
        *,
        status: str,
        tokens: tuple[int, int],
        cost: int,
        diagnostics: dict | None = None,
    ) -> None:
        if not self.task_id:
            return
        self.store.record_model_call(
            self.task_id,
            unit["id"],
            role=self.role,
            provider=self.provider,
            model=self.model_name,
            status=status,
            input_tokens=int(tokens[0] or 0),
            output_tokens=int(tokens[1] or 0),
            estimated_cost_micros=int(cost),
            image_sha256=image_sha,
            prompt_version=self.prompt_version,
            diagnostics=diagnostics,
        )

def _png_data_url(path: Path) -> str:
    raw = path.read_bytes()
    # 粗判 mime
    mime = "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def image_sha256_of(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
