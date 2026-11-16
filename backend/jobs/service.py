"""审查任务服务：创建/防重、检查点可重入、进度、续审探测、费用门闸。"""
from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable

from backend.jobs.codes import (
    CAT_INCOMPLETE,
    CAT_OUT_OF_SCOPE,
    CAT_UNREADABLE,
    ERR_COST_EXCEEDED,
    ERR_DATA_DIR,
    ERR_INVALID,
    ERR_NOT_FOUND,
    ERR_REVIEW_DISCLOSURE_REQUIRED,
    JobError,
    MSG_COST_PAUSED,
    MSG_RESUME_PROBE,
    MSG_REVIEW_DISCLOSURE_REQUIRED,
    MSG_RUNNING,
    OUTCOME_COMPLETED,
    OUTCOME_COST_PAUSED,
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_PENDING,
    OUTCOME_RUNNING,
    STEPS,
)
from backend.jobs.plugins import StepContext, StepRegistry, get_default_registry
from backend.models.presets import get_preset
from backend.progress import PROGRESS_FIELDS, new_progress, restart, tick, begin_phase, update_count, view_progress
from backend.settings import SettingsService
from backend.settings.codes import (
    ERR_CREDENTIAL_STORE,
    MESSAGES,
    MODEL_STATUS_DUAL,
    MODEL_STATUS_NONE,
    MODEL_STATUS_SINGLE,
)
from backend.settings.credentials import CredentialError
from backend.store import Repository
from backend.store.errors import ErrorCode, StoreError

# Shared across short-lived API service instances, but intentionally empty after process restart.
_ACTIVE_PROGRESS_TASKS: set[str] = set()
_REGION_FIELDS = frozenset({"region_processed_count", "region_total_count", "region_percent"})

# 进度/结果对外键集合（测试断言；禁止步骤明细/日志墙）
PROGRESS_PUBLIC_KEYS = frozenset({
    "task_id",
    "version_id",
    "status",
    "message",
    "percent",
    "issue_count",
    "incomplete_count",
    "unreadable_count",
    "out_of_scope_count",
    "cost_paused",
    "outcome",
    "already_running",
    "tolerance_snapshot",
}) | PROGRESS_FIELDS | _REGION_FIELDS

_ACTIVE_DB = frozenset({"待开始", "进行中"})
_RESUMABLE_DB = frozenset({"待开始", "进行中", "暂停", "失败"})


def _empty_checkpoints() -> dict[str, Any]:
    return {name: {"state": "pending"} for name in STEPS}


def _count_incomplete(items: list[dict[str, Any]]) -> dict[str, int]:
    incomplete = unreadable = out = 0
    for row in items:
        cat = (row or {}).get("category") or CAT_INCOMPLETE
        if cat == CAT_UNREADABLE:
            unreadable += 1
        elif cat == CAT_OUT_OF_SCOPE:
            out += 1
        else:
            incomplete += 1
    return {
        "incomplete_count": incomplete,
        "unreadable_count": unreadable,
        "out_of_scope_count": out,
    }


def _outcome_for(status: str, *, cost_paused: bool, counts: dict[str, int], issue_count: int) -> str:
    if cost_paused or status == "暂停":
        return OUTCOME_COST_PAUSED
    if status == "进行中":
        return OUTCOME_RUNNING
    if status == "待开始":
        return OUTCOME_PENDING
    if status == "失败":
        return OUTCOME_FAILED
    # 已完成：有任何未完成类计数 → 部分完成（不得冒充「没问题」）
    if (
        counts["incomplete_count"]
        or counts["unreadable_count"]
        or counts["out_of_scope_count"]
    ):
        return OUTCOME_PARTIAL
    return OUTCOME_COMPLETED


def _message_for(status: str, *, cost_paused: bool, outcome: str) -> str:
    if cost_paused or status == "暂停":
        return MSG_COST_PAUSED
    if status == "进行中":
        return MSG_RUNNING
    if outcome == OUTCOME_FAILED:
        return "审查未完成，已保留已有结果"
    if outcome == OUTCOME_PARTIAL:
        return "审查结束（部分完成），已保留已有结果"
    if status == "已完成":
        return "审查结束"
    if status == "待开始":
        return MSG_RESUME_PROBE
    return MSG_RUNNING


def _percent(checkpoints: dict[str, Any]) -> int:
    if not STEPS:
        return 0
    done = 0
    for name in STEPS:
        state = (checkpoints.get(name) or {}).get("state")
        if state in ("done", "skipped"):
            done += 1
    progress = (checkpoints.get("_meta") or {}).get("progress") or {}
    step = progress.get("step")
    fraction = 0.0
    if step in STEPS and (checkpoints.get(step) or {}).get("state") in {"running", "failed", "pending"}:
        total = progress.get("total_count", 0)
        if total:
            fraction = min(1.0, progress.get("processed_count", 0)/total)
    return round(100 * (done + fraction) / len(STEPS), 1)


def _issue_count(store: Repository, version_id: str) -> int:
    try:
        return len(store.list_records("issues", version_id=version_id))
    except Exception:
        return 0


def review_progress(store: Repository, task: dict[str, Any], now: datetime) -> dict[str, Any]:
    from backend.pending.current import current_incomplete, current_pending

    checkpoints = task.get("checkpoints") or {}
    version_id = task["version_id"]
    incomplete = current_incomplete(store, version_id, task.get("incomplete") or [])
    counts = _count_incomplete(incomplete)
    # Pending values are live coverage: correcting one must update progress without a new model run.
    counts["incomplete_count"] += sum(p.get("status") != "已确认" for p in current_pending(store, version_id))
    issue_count = _issue_count(store, version_id)
    meta = checkpoints.get("_meta") or {}
    cost_paused = bool(meta.get("cost_paused")) or task["status"] == "暂停"
    outcome = _outcome_for(task["status"], cost_paused=cost_paused, counts=counts, issue_count=issue_count)
    view = {
        "task_id": task["id"],
        "version_id": version_id,
        "status": task["status"],
        "message": _message_for(task["status"], cost_paused=cost_paused, outcome=outcome),
        "percent": 100 if task["status"] == "已完成" else min(99, _percent(checkpoints)),
        "issue_count": issue_count,
        **counts,
        "cost_paused": cost_paused,
        "outcome": outcome,
        "already_running": False,
        "tolerance_snapshot": deepcopy(task.get("tolerance_snapshot") or {}),
    }
    progress = meta.get("progress") or new_progress()
    view.update(view_progress(progress, now.timestamp(), status=task["status"], live=task["id"] in _ACTIVE_PROGRESS_TASKS))
    region_total = progress.get("region_total_count", 0) if progress.get("phase_key") == "recognizing" else 0
    region_done = progress.get("region_processed_count", 0) if region_total else 0
    view.update(region_total_count=region_total, region_processed_count=region_done,
                region_percent=round(region_done/region_total*100, 1) if region_total else None)
    return view


class JobService:
    """审查任务编排。默认同步跑 Mock 管道；可注入 registry / extract / settings。"""

    def __init__(
        self,
        store: Repository,
        *,
        settings_service: SettingsService | None = None,
        registry: StepRegistry | None = None,
        extract_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.settings_service = settings_service or SettingsService(store)
        self.registry = registry or get_default_registry()
        self.extract_factory = extract_factory
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.extras: dict[str, Any] = {}
        self._lock = threading.RLock()
        # 进程内正在跑的 version → task_id（防重；崩溃后不在此集合则可续审）
        self._running: dict[str, str] = {}

    # ---------- 查询 ----------
    def get_progress(self, task_id: str) -> dict[str, Any]:
        try:
            task = self.store.get("review_tasks", task_id)
        except StoreError as exc:
            raise JobError(ERR_NOT_FOUND, "审查任务不存在") from exc
        return self._public_view(task)

    def probe_incomplete(self, version_id: str) -> dict[str, Any]:
        """启动发现未完成（Q-10）。不自动 resume。"""
        task = self._find_resumable(version_id)
        if task is None:
            return {
                "has_incomplete": False,
                "task_id": None,
                "message": "",
                "version_id": version_id,
            }
        return {
            "has_incomplete": True,
            "task_id": task["id"],
            "message": MSG_RESUME_PROBE,
            "version_id": version_id,
            "status": task["status"],
        }

    # ---------- 开始 / 续审 / 防重 ----------
    @staticmethod
    def _saved_model_route(task: dict | None) -> dict | None:
        return (((task or {}).get("checkpoints") or {}).get("_meta") or {}).get("model_route")

    def _model_route(self, settings: dict, task: dict | None = None) -> dict:
        saved = self._saved_model_route(task)
        if saved is not None:
            return saved
        status = self.settings_service.model_status(settings)
        role = self.settings_service.effective_primary_role(settings) if status == MODEL_STATUS_SINGLE else "primary"
        return {
            "primary_model": settings.get(f"{role}_model") if status != MODEL_STATUS_NONE else None,
            "review_model": settings.get("review_model") if status == MODEL_STATUS_DUAL else None,
        }

    @staticmethod
    def _disclosure_id(version_id: str, task: dict | None, route: dict, retry_unit_id: str | None = None) -> str:
        from backend.extract.semantic import VERSION as SEMANTIC_VERSION
        payload = [version_id, (task or {}).get("id"), route, SEMANTIC_VERSION]
        if retry_unit_id is not None:
            meta = (((task or {}).get("checkpoints") or {}).get("_meta") or {})
            payload.extend([retry_unit_id, meta.get("single_page_attempt", 0)])
        payload = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _single_page_task(self, version_id: str, unit_id: str) -> dict:
        from backend.extract.service import can_retry_visual

        if not isinstance(unit_id, str) or not unit_id:
            raise JobError(ERR_INVALID, "请指定一张待续审的图纸")
        tasks = self.store.list_records("review_tasks", version_id=version_id)
        task = tasks[-1] if tasks else {}
        checkpoints = task.get("checkpoints") or {}
        pending = (checkpoints.get("_meta") or {}).get("single_page_retry")
        if pending == unit_id:
            return task
        unit = self.store.get("drawing_units", unit_id)
        if (pending or task.get("status") != "已完成" or (checkpoints.get("extract") or {}).get("state") != "done"
                or unit["version_id"] != version_id or not can_retry_visual(self.store.root, unit)):
            raise JobError(ERR_INVALID, "该图纸当前无法单页续审")
        return task

    def prepare_disclosure(self, version_id: str, *, retry_unit_id: str | None = None) -> dict:
        task = self._single_page_task(version_id, retry_unit_id) if retry_unit_id is not None else self._find_resumable(version_id)
        settings = self.store.get_settings()
        route = self._model_route(settings, task)
        pre = self.settings_service.precheck_start_review(settings={**settings, **route,
            **({"review_model": None} if retry_unit_id is not None else {})})
        if pre["ok"]:
            pre["disclosure_id"] = self._disclosure_id(version_id, task, route, retry_unit_id)
            pre["providers"] = [
                get_preset(slot["provider"]).display_name for role, slot in route.items()
                if slot and (retry_unit_id is None or role == "primary_model")
            ]
        return pre

    def start_review(
        self, version_id: str, *, run: bool = True, disclosure_id: str | None = None,
        retry_unit_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            running_id = self._running.get(version_id)
            if running_id:
                task = self.store.get("review_tasks", running_id)
                view = self._public_view(task)
                view["already_running"] = True
                return view

            # 进程内未跑、但库中有可续审任务 → 续审（含崩溃残留「进行中」）
            existing = (self._single_page_task(version_id, retry_unit_id) if retry_unit_id is not None
                        else self._find_resumable(version_id))
            settings = self.store.get_settings()
            route = self._model_route(settings, existing)
            if existing is None or existing["status"] == "暂停" or disclosure_id is not None or retry_unit_id is not None:
                pre = self.settings_service.precheck_start_review(settings={**settings, **route,
                    **({"review_model": None} if retry_unit_id is not None else {})})
                if not pre.get("ok"):
                    code = pre.get("code") or ERR_INVALID
                    view = self._public_view(existing) if existing else {"task_id": None}
                    return {
                        **view,
                        "ok": False,
                        "error": {
                            "code": code,
                            "message": pre.get("message") or MESSAGES.get(code, ""),
                        },
                        "model_status": pre.get("model_status"),
                        "already_running": False,
                    }
            if ((disclosure_id is not None or retry_unit_id is not None)
                    and disclosure_id != self._disclosure_id(version_id, existing, route, retry_unit_id)):
                raise JobError(ERR_REVIEW_DISCLOSURE_REQUIRED, MSG_REVIEW_DISCLOSURE_REQUIRED)

            if retry_unit_id is not None:
                checkpoints = deepcopy(existing["checkpoints"])
                meta = checkpoints.setdefault("_meta", {})
                if not meta.get("single_page_retry"):
                    for step in ("extract", "link", "rules_a", "rules_b", "finalize"):
                        checkpoints[step] = {"state": "pending"}
                    meta.update(single_page_retry=retry_unit_id, rules_reconcile_required=True,
                                rules_reconcile_trigger="single_page_retry", model_route=route,
                                single_page_attempt=int(meta.get("single_page_attempt", 0)) + 1)
                task = self.store.update_task(existing["id"], status="待开始", checkpoints=checkpoints,
                    incomplete=existing["incomplete"], estimated_cost_micros=existing["estimated_cost_micros"])
            elif existing is not None:
                task = existing
                checkpoints = task.get("checkpoints") or _empty_checkpoints()
                meta = checkpoints.setdefault("_meta", {})
                if "model_route" not in meta or (disclosure_id is not None and not meta.get("semantic_disclosed")):
                    meta.setdefault("model_route", route)
                    if disclosure_id is not None:
                        meta["semantic_disclosed"] = True
                        if (checkpoints.get("extract") or {}).get("state") == "done" and not checkpoints.get("semantic"):
                            for step in STEPS[STEPS.index("semantic"):]:
                                checkpoints[step] = {"state": "pending"}
                            meta.update(rules_reconcile_required=True, rules_reconcile_trigger="semantic_upgrade")
                    task = self.store.update_task(task["id"], status=task["status"], checkpoints=checkpoints,
                        incomplete=task["incomplete"], estimated_cost_micros=task["estimated_cost_micros"])
            else:
                checkpoints = _empty_checkpoints()
                checkpoints["_meta"] = {
                    "model_route": route,
                    "semantic_disclosed": disclosure_id is not None,
                    "rules_reconcile_required": any(previous["status"] == "已完成"
                        for previous in self.store.list_records("review_tasks", version_id=version_id)),
                }
                try:
                    task = self.store.create_task(version_id, checkpoints=checkpoints)
                except StoreError as exc:
                    if getattr(exc, "code", None) == ErrorCode.DATA_DIR_NOT_WRITABLE:
                        raise JobError(ERR_DATA_DIR, "数据目录不可写") from exc
                    raise JobError(ERR_INVALID, str(exc)) from exc
                from backend.revisions import RevisionService
                RevisionService(self.store).invalidate_comparison(version_id, task["id"])

            if run:
                self._running[version_id] = task["id"]

        if not run:
            view = self._public_view(task)
            view["already_running"] = False
            view["ok"] = True
            return view

        try:
            task = self.run_task(task["id"])
        finally:
            with self._lock:
                if self._running.get(version_id) == task["id"]:
                    self._running.pop(version_id, None)

        view = self._public_view(task)
        view["already_running"] = False
        view["ok"] = True
        # 新建与续审均返回进度形状；调用方可读 outcome/cost_paused
        return view

    # ---------- 管道 ----------
    def _prepare_partial_extract_retry(self, task):
        """A completed extraction stage can still contain retryable failed pages.

        Reset dependencies only for that explicit next run, never merely for
        import warnings, pending confirmations or an ordinary interrupted resume.
        The extractor's page/region caches retain already completed inference.
        """
        checkpoints = deepcopy(task.get("checkpoints") or {})
        if (checkpoints.get("extract") or {}).get("state") != "done":
            return task
        from backend.store.paths import safe_path
        retry_units = []
        for unit in self.store.list_records("drawing_units", version_id=task["version_id"]):
            meta = unit.get("metadata") or {}
            if meta.get("extract_status") != "incomplete" or meta.get("out_of_scope"):
                continue
            relative = meta.get("review_relative_path") or meta.get("preview_relative_path")
            if not relative:
                continue  # Unrendered source/DWG conversion is not a model retry.
            try:
                available = safe_path(self.store.root, relative).is_file()
            except (StoreError, ValueError, TypeError, OSError):
                available = False
            if available:
                retry_units.append(unit["id"])
        if not retry_units:
            return task
        for step in STEPS:
            checkpoints[step] = {"state":"pending"}
        meta = dict(checkpoints.get("_meta") or {})
        previous = meta.get("partial_extract_retry") or {}
        meta.update(rules_reconcile_required=True, rules_reconcile_trigger="partial_extract_retry",
                    partial_extract_retry={"attempt":int(previous.get("attempt", 0))+1,"unit_ids":retry_units})
        checkpoints["_meta"] = meta
        # Each step already replaces its own coverage only after passing the
        # cost gate. Preserve prior failure evidence if this retry pauses early.
        incomplete = list(task.get("incomplete") or [])
        self.store.update_task(task["id"], status=task["status"], checkpoints=checkpoints,
            incomplete=incomplete, estimated_cost_micros=task["estimated_cost_micros"])
        return self.store.get("review_tasks", task["id"])

    def run_task(self, task_id: str) -> dict[str, Any]:
        """Run the accepted task using its persisted model route."""
        task = self.store.get("review_tasks", task_id)
        route = self._saved_model_route(task)
        if route is None:
            raise JobError(ERR_REVIEW_DISCLOSURE_REQUIRED, MSG_REVIEW_DISCLOSURE_REQUIRED)
        single_page = (task["checkpoints"].get("_meta") or {}).get("single_page_retry")
        if not single_page:
            task = self._prepare_partial_extract_retry(task)
        version_id = task["version_id"]
        settings = self.store.get_settings()
        settings.update(route)
        model_status = self.settings_service.model_status(settings)
        checkpoints = dict(task.get("checkpoints") or {})
        if not checkpoints:
            checkpoints = _empty_checkpoints()
        incomplete = list(task.get("incomplete") or [])
        cost_micros = int(task.get("estimated_cost_micros") or 0)
        if single_page:
            # Recover any charged request written just before an interrupted checkpoint.
            logged = sum(row["estimated_cost_micros"] for row in self.store.list_records("model_calls", version_id=version_id)
                         if row["task_id"] == task_id)
            cost_micros = max(cost_micros, logged)
        cost_paused = bool((checkpoints.get("_meta") or {}).get("cost_paused"))
        if cost_paused and self._cost_allows_new_call():
            incomplete = [row for row in incomplete if row.get("reason") != "费用上限，审查已暂停"]
            cost_paused = False

        meta = dict(checkpoints.get("_meta") or {})
        progress = dict(meta.get("progress") or new_progress())
        restart(progress, self.clock().timestamp())
        meta["progress"] = progress
        checkpoints["_meta"] = meta
        _ACTIVE_PROGRESS_TASKS.add(task_id)
        # 进入运行态
        self.store.update_task(
            task_id,
            status="进行中",
            checkpoints=checkpoints,
            incomplete=incomplete,
            estimated_cost_micros=cost_micros,
        )

        try:
            # Register immutable original references before the session's before/after integrity snapshot.
            from backend.revisions import RevisionService
            RevisionService(self.store).prepare(version_id)
            with self.store.review_session(task_id):
                for step_name in STEPS:
                    cancel_event = self.extras.get("cancel_event")
                    if cancel_event is not None and cancel_event.is_set():
                        incomplete = self._merge_incomplete(incomplete, [{"unit_id": None,
                            "reason": "窗口关闭，审查中断，可继续", "category": CAT_INCOMPLETE,
                            "step": step_name}])
                        self._persist(task_id, "失败", checkpoints, incomplete, cost_micros)
                        return self.store.get("review_tasks", task_id)
                    # Once both local rule steps finish, reconcile this task's
                    # tolerance snapshot before any second-model calls. Persisted
                    # marker makes interruption/resume safe without replaying steps.
                    if step_name == "dual_review" and (checkpoints.get("_meta") or {}).get("rules_reconcile_required"):
                        from backend.rules.registry import reconcile_rule_results
                        reconciled = reconcile_rule_results(self.store, version_id=version_id,
                            task_id=task_id, settings=settings,
                            trigger=(checkpoints.get("_meta") or {}).get("rules_reconcile_trigger", "new_task"))
                        incomplete = [row for row in incomplete if row.get("step") != "rules_reconciliation"]
                        incomplete = self._merge_incomplete(incomplete,
                            [{**row, "step": "rules_reconciliation"} for row in reconciled["incomplete"]])
                        meta = dict(checkpoints.get("_meta") or {})
                        meta["rules_reconcile_required"] = False
                        checkpoints["_meta"] = meta
                        self._persist(task_id, "进行中", checkpoints, incomplete, cost_micros)
                    cp = dict(checkpoints.get(step_name) or {"state": "pending"})
                    if cp.get("state") in ("done", "skipped") or (single_page and step_name in {"semantic", "dual_review"}):
                        continue

                    # single → 跳过 dual_review（Q-03）
                    if step_name == "dual_review" and model_status == MODEL_STATUS_SINGLE:
                        checkpoints[step_name] = {
                            "state": "skipped",
                            "model_calls": 0,
                            "review_model_calls": 0,
                            "reason": "single_model",
                        }
                        self._persist(task_id, "进行中", checkpoints, incomplete, cost_micros)
                        continue

                    plugin = self.registry.get(step_name)
                    if plugin is None:
                        checkpoints[step_name] = {"state": "skipped", "reason": "unregistered"}
                        self._persist(task_id, "进行中", checkpoints, incomplete, cost_micros)
                        continue

                    # 费用门闸：需模型的步骤，调用前检查；超限则暂停、新调用=0
                    if getattr(plugin, "uses_model", False):
                        if not self._cost_allows_new_call():
                            cost_paused = True
                            meta = dict(checkpoints.get("_meta") or {})
                            meta.update({"cost_paused": True, "pause_step": step_name})
                            checkpoints["_meta"] = meta
                            checkpoints[step_name] = {"state": "pending", "reason": "cost_paused"}
                            incomplete = self._merge_incomplete(
                                incomplete,
                                [{
                                    "unit_id": None,
                                    "reason": "费用上限，审查已暂停",
                                    "category": CAT_INCOMPLETE,
                                }],
                            )
                            self._persist(task_id, "暂停", checkpoints, incomplete, cost_micros)
                            return self.store.get("review_tasks", task_id)

                    checkpoints[step_name] = {**cp, "state": "running"}
                    self._begin_progress_step(checkpoints, step_name)
                    self._persist(task_id, "进行中", checkpoints, incomplete, cost_micros)

                    merged_extras = {}
                    merged_extras.update(dict((checkpoints.get("_meta") or {}).get("extras") or {}))
                    merged_extras.update(self.extras)
                    merged_extras["semantic_disclosed"] = bool((checkpoints.get("_meta") or {}).get("semantic_disclosed"))
                    if single_page:
                        merged_extras["single_page_retry"] = single_page
                    merged_extras.setdefault("primary_client_factory",
                        lambda: self._model_client_for_slot("primary", route["primary_model"]))
                    merged_extras.setdefault("review_client_factory",
                        lambda: self._model_client_for_slot("review", route["review_model"]))
                    def report_progress(processed, total, *, unit=None):
                        state = checkpoints["_meta"]["progress"]
                        if unit is not None:
                            state["progress_unit"] = unit
                        update_count(state, self.clock().timestamp(), processed, total)
                        self._persist(task_id, "进行中", checkpoints, incomplete, cost_micros)
                    def report_region_progress(processed, total):
                        state = checkpoints["_meta"]["progress"]
                        state["region_total_count"] = max(0, int(total))
                        state["region_processed_count"] = min(max(0, int(processed)), state["region_total_count"])
                        self._persist(task_id, "进行中", checkpoints, incomplete, cost_micros)
                    merged_extras["progress_callback"] = report_progress
                    merged_extras["region_progress_callback"] = report_region_progress
                    # Retry replaces the previous attempt's incomplete records for this step.
                    targeted_extract = single_page and step_name == "extract"
                    if not targeted_extract:
                        incomplete = [row for row in incomplete if row.get("step") != step_name]
                    ctx = StepContext(
                        store=self.store,
                        task_id=task_id,
                        version_id=version_id,
                        model_status=model_status,
                        settings=settings,
                        checkpoints=checkpoints,
                        incomplete=list(incomplete),
                        primary_model=settings.get("primary_model"),
                        review_model=settings.get("review_model"),
                        extract_factory=self.extract_factory,
                        allow_model_call=self._cost_allows_new_call,
                        extras=merged_extras,
                    )
                    try:
                        result = plugin.run(ctx)
                    except Exception as exc:  # noqa: BLE001 — 步骤失败标未完成，不崩进程
                        checkpoints[step_name] = {
                            "state": "failed",
                            "error": type(exc).__name__,
                        }
                        incomplete = self._merge_incomplete(
                            incomplete,
                            [{
                                "unit_id": None,
                                "reason": "本次确认的模型凭据不可用，审查未完成"
                                if isinstance(exc, CredentialError) else f"步骤失败：{step_name}",
                                "category": CAT_INCOMPLETE,
                                "step": step_name,
                            }],
                        )
                        meta = dict(checkpoints.get("_meta") or {})
                        meta["step_error"] = type(exc).__name__
                        checkpoints["_meta"] = meta
                        self._persist(task_id, "失败", checkpoints, incomplete, cost_micros)
                        return self.store.get("review_tasks", task_id)

                    if result.state == "failed" and (result.detail or {}).get("reason") == "cost_exceeded_before_call":
                        cost_paused = True
                        cost_micros += int(result.estimated_cost_micros or 0)
                        meta = dict(checkpoints.get("_meta") or {})
                        meta.update({"cost_paused": True, "pause_step": step_name})
                        checkpoints["_meta"] = meta
                        incomplete = self._merge_incomplete(incomplete, [{**row, "step": step_name} for row in result.incomplete])
                        self._persist(task_id, "暂停", checkpoints, incomplete, cost_micros)
                        return self.store.get("review_tasks", task_id)

                    if targeted_extract:
                        incomplete = [row for row in incomplete if row.get("step") != "extract" or row.get("unit_id") != single_page]
                    incomplete = self._merge_incomplete(incomplete, [{**row, "step": step_name} for row in result.incomplete])
                    cost_micros += int(result.estimated_cost_micros or 0)
                    if result.state in {"done", "skipped"}:
                        progress = checkpoints["_meta"]["progress"]
                        update_count(progress, self.clock().timestamp(), progress.get("total_count", 0))
                    checkpoints[step_name] = {
                        "state": result.state,
                        "model_calls": int(result.model_calls or 0),
                        "review_model_calls": int(result.review_model_calls or 0),
                        "detail": result.detail or {},
                    }
                    if result.state == "failed":
                        self._persist(task_id, "失败", checkpoints, incomplete, cost_micros)
                        return self.store.get("review_tasks", task_id)

                    self._persist(task_id, "进行中", checkpoints, incomplete, cost_micros)

                # 全部步骤结束：有未完成类 → 已完成 + outcome=partial（W6-Q3=A 标题仍「审查完成」归 UI）
                counts = _count_incomplete(incomplete)
                final_status = "已完成"
                if any(
                    (checkpoints.get(name) or {}).get("state") == "failed"
                    for name in STEPS
                ):
                    final_status = "失败"
                meta = dict(checkpoints.get("_meta") or {})
                if final_status == "已完成":
                    meta.pop("single_page_retry", None)
                meta["cost_paused"] = False if final_status == "已完成" else meta.get("cost_paused", False)
                meta["counts"] = {
                    **counts,
                    "issue_count": _issue_count(self.store, version_id),
                }
                checkpoints["_meta"] = meta
                self._persist(task_id, final_status, checkpoints, incomplete, cost_micros)
                if final_status == "已完成":
                    from backend.revisions import RevisionService
                    RevisionService(self.store).finalize(version_id, task_id)
                return self.store.get("review_tasks", task_id)
        except StoreError as exc:
            incomplete = self._merge_incomplete(
                incomplete,
                [{
                    "unit_id": None,
                    "reason": "存储错误，审查中断",
                    "category": CAT_INCOMPLETE,
                }],
            )
            checkpoints.setdefault("_meta", {})["store_error"] = str(getattr(exc, "code", "STORAGE"))
            self._persist(task_id, "失败", checkpoints, incomplete, cost_micros)
            return self.store.get("review_tasks", task_id)
        finally:
            _ACTIVE_PROGRESS_TASKS.discard(task_id)

    def _begin_progress_step(self, checkpoints, step_name):
        phases = {"extract":("recognizing", "正在识别图纸", 0, "张图"),
            "semantic":("checking", "正在分析图纸文字与表格语义", 0, "组图纸"),
            "link":("linking", "正在关联相关图纸", 1, "个步骤"),
            "rules_a":("checking", "正在核对目录、尺寸与标高", 1, "个步骤"),
            "rules_b":("checking", "正在核对编号、规格、数量与详图", 1, "个步骤"),
            "dual_review":("second_review", "正在用第二模型复核", 0, "条问题"),
            "finalize":("finalizing", "正在整理审查结果", 1, "个步骤")}
        key, label, total, unit = phases[step_name]
        state = checkpoints["_meta"].setdefault("progress", new_progress())
        begin_phase(state, self.clock().timestamp(), key, label, total, unit)
        state.update(step=step_name, region_processed_count=0, region_total_count=0)

    def model_client(self, role: str):
        """Client for standalone local validation tools."""
        return self._model_client_for_slot(role, self.store.get_settings().get(f"{role}_model"))

    def _model_client_for_slot(self, role: str, slot: dict | None):
        if not slot:
            raise CredentialError(ERR_CREDENTIAL_STORE, "本次审查未配置此模型")
        ref = slot.get("credential_ref")
        secret = self.settings_service.credentials.resolve(ref)
        if ref and not secret:
            raise CredentialError(ERR_CREDENTIAL_STORE, "本次确认的模型凭据不可用")
        client = self.settings_service._make_client(slot, secret)
        from backend.models.openai_compat import OpenAICompatClient
        if isinstance(client, OpenAICompatClient):
            # Real vision work can wait longer for an answer, while transport
            # failures stay bounded. Region-level logic owns visible, metered retries.
            client.timeout_sec = 300.0 if role == "primary" else 180.0
            client.connect_timeout_sec = 15.0
            client.write_timeout_sec = 60.0
            client.pool_timeout_sec = 15.0
            client.max_retries = 0
        return client

    def retry_issue_review(self, issue_id: str):
        from backend.review import ReviewService
        issue = self.store.get("issues", issue_id)
        tasks = self.store.list_records("review_tasks", version_id=issue["version_id"])
        if not tasks:
            raise JobError(ERR_NOT_FOUND, "审查任务不存在")
        task = max(tasks, key=lambda row: row.get("created_at", ""))
        route = self._saved_model_route(task)
        if route is None:
            raise JobError(ERR_REVIEW_DISCLOSURE_REQUIRED, MSG_REVIEW_DISCLOSURE_REQUIRED)
        if not route.get("review_model"):
            raise JobError(ERR_INVALID, "单模型任务不执行第二模型复核")
        service = ReviewService(self.store, task_id=task["id"], primary_model=route["primary_model"],
            review_model=route["review_model"], client=self.extras.get("review_client"),
            client_factory=self.extras.get("review_client_factory")
            or (lambda: self._model_client_for_slot("review", route["review_model"])),
            allow_model_call=self._cost_allows_new_call)
        result = service.retry_issue(issue_id)
        checkpoints = dict(task.get("checkpoints") or {})
        incomplete = list(task.get("incomplete") or [])
        if not result.cost_paused:
            incomplete = [row for row in incomplete if row.get("issue_id") != issue_id]
            incomplete.extend({**row, "step": "dual_review"} for row in result.incomplete)
        cp = dict(checkpoints.get("dual_review") or {})
        cp["model_calls"] = int(cp.get("model_calls", 0)) + result.model_calls
        cp["review_model_calls"] = int(cp.get("review_model_calls", 0)) + result.model_calls
        still_failed = any(row.get("step") == "dual_review" for row in incomplete)
        cp["state"] = "pending" if result.cost_paused else ("failed" if still_failed else "done")
        checkpoints["dual_review"] = cp
        meta = dict(checkpoints.get("_meta") or {})
        meta["cost_paused"] = result.cost_paused
        checkpoints["_meta"] = meta
        status = "暂停" if result.cost_paused else "失败"
        self._persist(task["id"], status, checkpoints, incomplete,
            int(task.get("estimated_cost_micros", 0)) + result.estimated_cost_micros)
        if not result.cost_paused and not still_failed:
            self.run_task(task["id"])
        return {"issue": self.store.get("issues", issue_id), "incomplete": result.incomplete,
            "model_calls": result.model_calls, "cost_paused": result.cost_paused}

    def _persist(
        self,
        task_id: str,
        status: str,
        checkpoints: dict[str, Any],
        incomplete: list[dict[str, Any]],
        cost_micros: int,
    ) -> None:
        meta = checkpoints.setdefault("_meta", {})
        state = meta.setdefault("progress", new_progress())
        tick(state, self.clock().timestamp(), running=status == "进行中")
        self.store.update_task(
            task_id,
            status=status,
            checkpoints=checkpoints,
            incomplete=incomplete,
            estimated_cost_micros=int(cost_micros),
        )

    def _cost_allows_new_call(self) -> bool:
        settings = self.store.get_settings()
        limit = settings.get("monthly_limit_yuan")
        if limit is None:
            return False
        used = int(self.settings_service.review_cost_reader(self.clock()))
        limit_micros = int(round(float(limit) * 1_000_000))
        return used < limit_micros

    @staticmethod
    def _merge_incomplete(
        existing: list[dict[str, Any]],
        more: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        out = list(existing)
        seen = {
            (row.get("unit_id"), row.get("category"), row.get("reason"))
            for row in out
        }
        for row in more:
            key = (row.get("unit_id"), row.get("category"), row.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    def _find_resumable(self, version_id: str) -> dict[str, Any] | None:
        try:
            tasks = self.store.list_records("review_tasks", version_id=version_id)
        except StoreError:
            return None
        # 新到旧
        for task in reversed(tasks):
            if task["status"] not in _RESUMABLE_DB:
                continue
            if task["status"] == "已完成":
                continue
            checkpoints = task.get("checkpoints") or {}
            unfinished = any(
                (checkpoints.get(name) or {}).get("state") not in ("done", "skipped")
                for name in STEPS
            )
            has_incomplete = bool(task.get("incomplete"))
            if task["status"] in {"暂停", "失败", "进行中", "待开始"} and (unfinished or has_incomplete or task["status"] != "已完成"):
                # 已完成不在此；进行中/暂停/失败/待开始均可探测
                if task["status"] == "待开始" and not unfinished and not has_incomplete:
                    # 空待开始也算可续（刚创建）
                    return task
                if unfinished or has_incomplete or task["status"] in {"进行中", "暂停", "失败"}:
                    return task
        return None

    def _public_view(self, task: dict[str, Any]) -> dict[str, Any]:
        return review_progress(self.store, task, self.clock())
