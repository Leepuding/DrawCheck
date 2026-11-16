"""本机可信路径导入服务：内存任务表、独立工作线程与可序列化结果。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import threading
import time
from uuid import uuid4

from backend.parsers.base import ParseInput
from backend.parsers.pdf import PDFError, PDFParser
from backend.parsers.dwg.parser import DwgParser
from backend.store import Evidence
from backend.store import ErrorCode, Repository, StoreError
from backend.store.errors import translate_error
from backend.store.paths import data_root, safe_path

from backend.progress import new_progress, restart, tick, begin_phase, update_count, view_progress

from .archives import ImportLimits, ImportProblem, collect, expand
from .diagnostics import present_import_diagnostics


def _empty_result() -> dict:
    return {"project_id": None, "version_id": None, "file_count": 0, "unit_count": 0,
            "skipped_count": 0, "files": [], "drawings": [], "unreadable": [],
            "summary": "已导入 0 个文件 · 拆出 0 张图", "duplicate_message": ""}


class ImportService:
    """不挂 HTTP；路径只能来自本机可信文件选择器，不能直接接受远程任意路径。"""

    def __init__(self, data_dir: Path | str | None = None, *, limits: ImportLimits | None = None,
                 pdf_parser: PDFParser | None = None, dwg_parser=None, clock=None):
        self.root = Path(data_dir).expanduser().resolve() if data_dir is not None else data_root().resolve()
        self.limits = limits or ImportLimits()
        self.pdf_parser = pdf_parser or PDFParser()
        self.dwg_parser = dwg_parser or DwgParser()
        # 单服务串行导入，避免同版去重、迁移和目录写入之间的竞争。
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drawcheck-import")
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._futures = {}
        self.clock = clock or time.monotonic
        self._progress = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self._executor.shutdown(wait=True)

    def submit(self, paths, *, project_name: str = "导入项目", version_name: str = "第一版",
               version_id: str | None = None) -> str:
        """默认新建项目和版本；给定 version_id 则追加到现有最新版本。"""
        if isinstance(paths, (str, Path)):
            paths = [paths]
        sources = [Path(path).expanduser().absolute() for path in paths]
        identifier = uuid4().hex
        with self._lock:
            self._jobs[identifier] = {"job_id": identifier, "status": "queued", "phase": "等待导入",
                                      "processed_count": 0, "total_count": 0, "current_file": None,
                                      "result": None, "error": None}
            self._progress[identifier] = new_progress()
            self._futures[identifier] = self._executor.submit(
                self._execute, identifier, sources, project_name, version_name, version_id)
        return identifier

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            job = deepcopy(self._jobs[job_id])
            job.update(view_progress(self._progress[job_id], self.clock(), status=job["status"], live=job["status"] == "running"))
            total, done = job["total_count"], job["processed_count"]
            job["percent"] = 100 if job["status"] == "completed" else min(99, round(done/total*100, 1)) if total else 0
        job["result"] = present_import_diagnostics(job["result"])
        return job

    def wait(self, job_id: str, timeout: float | None = None) -> dict:
        with self._lock:
            future = self._futures[job_id]
        future.result(timeout=timeout)
        return self.get_job(job_id)

    def _update(self, job_id: str, **changes):
        with self._lock:
            self._jobs[job_id].update(deepcopy(changes))
            state = self._progress[job_id]
            status = self._jobs[job_id]["status"]
            tick(state, self.clock(), running=status == "running")
            if "processed_count" in changes:
                update_count(state, self.clock(), changes["processed_count"], changes.get("total_count"))
                if status != "running":
                    tick(state, self.clock(), running=False)

    def _execute(self, identifier, paths, project_name, version_name, version_id):
        result = _empty_result()
        try:
            with self._lock:
                restart(self._progress[identifier], self.clock())
                begin_phase(self._progress[identifier], self.clock(), "collecting", "正在收集文件", unit="个文件")
            self._update(identifier, status="running", phase="正在收集文件")
            items = collect(paths, self.limits)
            with self._lock:
                begin_phase(self._progress[identifier], self.clock(), "expanding", "正在展开压缩包", unit="个文件")
            self._update(identifier, total_count=0, phase="正在展开压缩包")
            with tempfile.TemporaryDirectory(prefix="drawcheck-import-") as temporary:
                items = expand(items, Path(temporary), self.limits)
                with self._lock:
                    begin_phase(self._progress[identifier], self.clock(), "importing", "正在导入图纸", len(items), "个文件")
                self._update(identifier, total_count=len(items))
                # 所有成员均通过整批限额检查后才创建数据库、项目与版本。
                with Repository(self.root) as store:
                    if version_id is None:
                        project = store.create_project(project_name)
                        version = store.create_version(project["id"], version_name)
                    else:
                        version = store.get("drawing_versions", version_id)
                        # 提前拒绝旧版本，即使整批只有重复或读不了的文件。
                        store._writable_version(version_id)
                    result.update(project_id=version["project_id"], version_id=version["id"])
                    for index, item in enumerate(items):
                        self._update(identifier, phase="正在导入图纸", current_file=Path(item.name.replace("\\", "/")).name)
                        self._import_one(store, version["id"], item, result)
                        self._summarize(result)
                        self._persist_result(store, result)
                        self._update(identifier, processed_count=index + 1, result=result)
                    store.verify_originals(version["id"])
                    from .project_codes import project_code_notice
                    result["project_code_notice"] = project_code_notice(store, version["id"], result["drawings"])
                    self._persist_result(store, result)
            self._summarize(result)
            self._update(identifier, status="completed", phase="导入已结束", current_file=None, result=result)
        except ImportProblem as exc:
            # 超限或快照期间输入变化在入库前发现，整批没有新增项目、版本、原图或单元。
            self._update(identifier, status="rejected", phase="未导入", result=result,
                         current_file=None, error={"code": exc.code, "message": str(exc)})
        except (StoreError, OSError) as exc:
            error = exc if isinstance(exc, StoreError) else translate_error(exc)
            self._summarize(result)
            self._update(identifier, status="failed", phase="导入中断", result=result, current_file=None,
                         error={"code": error.code.value, "message": str(error)})
        except Exception:
            # 保留已写入数量，但失败绝不能伪装成整批完成；不向用户返回路径或异常原文。
            self._summarize(result)
            self._update(identifier, status="failed", phase="导入中断", result=result, current_file=None,
                         error={"code": "IMPORT_FAILED", "message": "导入失败，请重试"})

    @staticmethod
    def _persist_result(store, result):
        """Durable import diagnostics for history and report generation (no source paths)."""
        relative = f"projects/{result['project_id']}/cache/import-{result['version_id']}.json"
        destination = safe_path(store.root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        snapshot = deepcopy(result)
        if destination.is_file():
            previous = json.loads(destination.read_text(encoding="utf-8"))
            replaced_names = {row["name"] for row in result.get("files", []) if row.get("status") in {"imported", "partial"}}
            previous["unreadable"] = [row for row in previous.get("unreadable", []) if row.get("name") not in replaced_names]
            for field in ("unreadable", "drawings", "files"):
                merged = []
                seen = set()
                for row in previous.get(field, []) + result.get(field, []):
                    key = (row.get("unit_id"),) if field == "drawings" else (row.get("original_id"), row.get("name"), row.get("code"), row.get("page"))
                    if key not in seen:
                        seen.add(key)
                        merged.append(row)
                snapshot[field] = merged
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        temporary.replace(destination)

    @staticmethod
    def _summarize(result):
        result["unit_count"] = len(result["drawings"])
        result["summary"] = f"已导入 {result['file_count']} 个文件 · 拆出 {result['unit_count']} 张图"
        result["duplicate_message"] = f"已跳过 {result['skipped_count']} 个重复文件" if result["skipped_count"] else ""

    def _import_one(self, store, version_id, item, result):
        def unreadable(code, reason, *, original_id=None, page=None):
            result["unreadable"].append({"name": item.name, "code": code, "reason": reason,
                                         "original_id": original_id, "page": page})

        if item.code:
            unreadable(item.code, item.reason)
            return
        format = item.format
        if format not in ("pdf", "dwg"):
            if format == "empty":
                unreadable("EMPTY_FILE", "文件为空，读不了")
            elif format != "dxf" and item.path.suffix.lower() in (".pdf", ".dwg", ".zip", ".rar", ".7z"):
                unreadable("FILE_CORRUPT", "文件损坏，读不了")
            else:
                unreadable("UNSUPPORTED_FORMAT", "不支持的格式")
            return
        imported = store.import_original(version_id, item.path)
        original = imported["record"]
        row = {"name": item.name, "original_id": original["id"], "format": format,
               "sha256": original["sha256"], "status": "pending_parse", "status_label": "待解析",
               "unit_count": 0}
        result["files"].append(row)
        if imported["reused"]:
            row.update(status="skipped", status_label="重复文件，已跳过")
            result["skipped_count"] += 1
            return
        if format == "dwg":
            # The original is imported even when its conversion fails; the failure remains explicit below.
            result["file_count"] += 1
        cache_relative = f"projects/{original['project_id']}/cache/{original['id']}"
        cache = safe_path(store.root, cache_relative)
        try:
            parser = self.dwg_parser if format == "dwg" else self.pdf_parser
            parsed = parser.parse(ParseInput(original["id"], store.original_path(original["id"]),
                                             original["sha256"], cache, format))
        except PDFError as exc:
            row.update(status="unreadable", status_label="读不了")
            unreadable(exc.code, str(exc), original_id=original["id"])
            return
        for unit in parsed.units:
            metadata = {}
            for field in ("native_measurements", "native_signals", "native_connections"):
                if value := getattr(unit, field, ()):
                    metadata[field] = value
            for field in ("preview_relative_path", "review_relative_path", "page_relative_path"):
                relative = getattr(unit, field, None)
                if relative:
                    path = safe_path(cache, relative)
                    if not path.is_file():
                        raise StoreError(ErrorCode.INVALID_PATH, "解析器缓存文件不存在")
                    metadata[field] = path.relative_to(store.root).as_posix()
            metadata.update(width_points=getattr(unit, "width_points", 0),
                            height_points=getattr(unit, "height_points", 0))
            name = unit.name if format == "dwg" else f"{item.path.name} · 第 {unit.ordinal} 页"
            record = store.create_unit(version_id, original["id"], kind=unit.kind, ordinal=unit.ordinal,
                                       name=name, drawing_number=unit.drawing_number, metadata=metadata)
            # Preserve the original CAD/PDF text and its parser provenance.
            for text in unit.texts:
                store.add_fact(version_id, Evidence(record["id"], text.source, text.text, text.bbox))
            result["drawings"].append({"unit_id": record["id"], "original_id": original["id"],
                                       "source_original": item.name, "ordinal": unit.ordinal,
                                       "drawing_number": record["drawing_number"], "name": name,
                                       **{k: v for k, v in metadata.items() if not k.startswith("native_")}})
        failures = getattr(parsed, "failures", ())
        for failure in failures:
            unreadable(failure.code, failure.reason, original_id=original["id"], page=failure.page)
        if not failures:
            reasons = ["；".join(parsed.incomplete)] if format == "dwg" and parsed.incomplete else parsed.incomplete
            for reason in reasons:
                unreadable("DWG_INCOMPLETE" if format == "dwg" else "PDF_INCOMPLETE", reason, original_id=original["id"])
        row["unit_count"] = len(parsed.units)
        if parsed.units and format != "dwg":
            result["file_count"] += 1
        row.update(status="partial" if parsed.units and parsed.incomplete else ("imported" if parsed.units else "unreadable"),
                   status_label="部分页面读不了" if parsed.units and parsed.incomplete else ("已导入" if parsed.units else "读不了"))
