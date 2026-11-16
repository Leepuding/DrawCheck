"""步骤插件 Protocol 与注册表（DC-11/12/13 挂载点）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class StepResult:
    """单步执行结果。state: done | skipped | failed"""
    state: str
    incomplete: list[dict[str, Any]] = field(default_factory=list)
    model_calls: int = 0
    review_model_calls: int = 0
    estimated_cost_micros: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepContext:
    """步骤执行上下文；插件只读业务输入，经 JobService 写回检查点。"""
    store: Any
    task_id: str
    version_id: str
    model_status: str
    settings: dict[str, Any]
    checkpoints: dict[str, Any]
    incomplete: list[dict[str, Any]]
    primary_model: dict[str, Any] | None
    review_model: dict[str, Any] | None
    extract_factory: Callable[[], Any] | None = None
    allow_model_call: Callable[[], bool] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class StepPlugin(Protocol):
    """DC-11/12/13 实现此协议并 register 到 StepRegistry。"""

    name: str
    uses_model: bool

    def run(self, ctx: StepContext) -> StepResult: ...


class StepRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, StepPlugin] = {}

    def register(self, plugin: StepPlugin) -> None:
        self._plugins[plugin.name] = plugin

    def get(self, name: str) -> StepPlugin | None:
        return self._plugins.get(name)

    def names(self) -> list[str]:
        return list(self._plugins.keys())


class NoOpPlugin:
    """无业务实现时跳过并写 checkpoint=skipped。"""

    uses_model = False

    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult(state="skipped", detail={"reason": "no_plugin"})


class ExtractPlugin:
    """挂接 DC-09 ExtractService.extract_version（默认 Mock）。"""

    name = "extract"
    uses_model = True

    def run(self, ctx: StepContext) -> StepResult:
        from backend.jobs.codes import CAT_INCOMPLETE, CAT_OUT_OF_SCOPE, CAT_UNREADABLE
        from backend.catalog import CatalogService
        from backend.extract.local_sheet import LocalSheetReader

        if ctx.allow_model_call is not None and not ctx.allow_model_call():
            return StepResult(state="failed", detail={"reason": "cost_exceeded_before_call"})

        single_page = ctx.extras.get("single_page_retry")
        factory = ctx.extract_factory
        local_reader = LocalSheetReader(ctx.store) if factory is None else None
        if factory is None:
            from backend.extract import ExtractService

            extract = ExtractService(ctx.store, task_id=ctx.task_id, use_mock=False,
                provider=(ctx.primary_model or {}).get("provider", ""),
                model_name=(ctx.primary_model or {}).get("model", ""),
                model_base_url=(ctx.primary_model or {}).get("base_url", ""),
                model_credential_ref=(ctx.primary_model or {}).get("credential_ref", ""),
                model_factory=ctx.extras.get("primary_client_factory"), allow_model_call=ctx.allow_model_call,
                stream=(ctx.primary_model or {}).get("provider") == "siliconflow",
                **({"format_retries": 0} if single_page else {}))
        else:
            extract = factory()

        report = ctx.extras.get("progress_callback")
        report_region = ctx.extras.get("region_progress_callback")
        extract.region_progress_callback = report_region
        prior_cost = sum(row.get("estimated_cost_micros", 0) for row in ctx.store.list_records("model_calls", version_id=ctx.version_id) if row.get("task_id") == ctx.task_id)
        from backend.revisions import RevisionService
        plan = None if single_page else RevisionService(ctx.store).prepare(ctx.version_id)
        paused = False
        catalog = CatalogService(ctx.store)

        def sync_directory(item):
            if not hasattr(item, "as_dict"):
                return
            reading = None
            if local_reader:
                reading = local_reader.read_unit(item.unit_id,
                    catalog_hint=bool(item.catalog.get("is_catalog_sheet")),
                    material_hint=any("材料表" in (block.get("name") or "") for block in item.title_blocks))
            if item.status == "ok" or (reading and any(reading.get(key) for key in ("title", "catalog", "materials", "engineering"))):
                catalog.apply_extract_result(item, local_reading=reading)

        if not single_page and not plan and ctx.extract_factory is not None and not hasattr(extract, "extract_unit"):
            total = len(ctx.store.list_records("drawing_units", version_id=ctx.version_id))
            if callable(report):
                report(0, total, unit="张图")
            results = extract.extract_version(ctx.version_id, persist=True)
            for result in results:
                sync_directory(result)
            if callable(report):
                report(len(results), total, unit="张图")
        else:
            results = []
            if single_page:
                unit_ids = [single_page]
            else:
                unit_ids = plan["changed_unit_ids"] if plan else [unit["id"] for unit in ctx.store.list_records("drawing_units", version_id=ctx.version_id)]
            if callable(report):
                report(0, len(unit_ids), unit="张图")
            for index, uid in enumerate(unit_ids):
                if callable(report_region):
                    report_region(0, 0)
                if ctx.allow_model_call is not None and not ctx.allow_model_call():
                    paused = True
                    break
                result = extract.extract_unit(uid, persist=True)
                results.append(result)
                sync_directory(result)
                if getattr(result, "cost_paused", False):
                    paused = True
                    break
                if callable(report):
                    report(index + 1, len(unit_ids), unit="张图")
        incomplete: list[dict[str, Any]] = []
        scope_candidates = set(ctx.extras.get("force_out_of_scope_unit_ids") or [])
        model_calls = 0
        for item in results:
            model_calls += int(getattr(item, "model_calls", 0) or 0)
            status = getattr(item, "status", None) or (item.get("status") if isinstance(item, dict) else None)
            if status == "ok":
                continue
            reason = (
                getattr(item, "incomplete_reason", None)
                or (item.get("incomplete_reason") if isinstance(item, dict) else None)
                or "未完成"
            )
            unit_id = getattr(item, "unit_id", None) or (item.get("unit_id") if isinstance(item, dict) else None)
            category = CAT_INCOMPLETE
            text = str(reason)
            # 单元 metadata 或原因文案可标范围外 / 读不了
            meta = {}
            try:
                unit = ctx.store.get("drawing_units", unit_id) if unit_id else None
                meta = (unit or {}).get("metadata") or {}
            except Exception:
                meta = {}
            if (
                meta.get("out_of_scope")
                or meta.get("review_scope") == "out"
                or "不在审查范围" in text
                or "范围外" in text
            ):
                scope_candidates.add(unit_id)
                # Scope can coexist with a genuine unreadable or failed page.
                if "不在审查范围" in text or "范围外" in text:
                    continue
            if meta.get("unreadable") or "读不了" in text or "无审查图" in text:
                category = CAT_UNREADABLE
            incomplete.append({"unit_id": unit_id, "reason": text, "category": category})

        if plan and not paused:
            RevisionService(ctx.store).rematch(ctx.version_id)

        # A page retry keeps source-level and other pages' saved coverage.
        for unit in ([] if single_page else ctx.store.list_records("drawing_units", version_id=ctx.version_id)):
            meta = unit.get("metadata") or {}
            if unit["id"] in scope_candidates or meta.get("out_of_scope") or meta.get("review_scope") == "out":
                incomplete.append({"unit_id": unit["id"], "reason": "不在审查范围", "category": CAT_OUT_OF_SCOPE})

        # Import failures have no drawing unit, but must remain visible as uncovered scope.
        import json
        from backend.store.paths import safe_path
        version = ctx.store.get("drawing_versions", ctx.version_id)
        manifest = safe_path(ctx.store.root, f"projects/{version['project_id']}/cache/import-{ctx.version_id}.json")
        if not single_page and manifest.is_file():
            for row in json.loads(manifest.read_text(encoding="utf-8")).get("unreadable", []):
                incomplete.append({"unit_id": None, "original_id": row.get("original_id"), "reason": f"{row.get('name', '')}：{row.get('reason') or '读不了'}", "category": CAT_UNREADABLE})

        state = "failed" if paused else "done"
        return StepResult(
            state=state,
            incomplete=incomplete,
            model_calls=model_calls,
            estimated_cost_micros=sum(row.get("estimated_cost_micros", 0) for row in ctx.store.list_records("model_calls", version_id=ctx.version_id) if row.get("task_id") == ctx.task_id) - prior_cost,
            detail={"unit_results": len(results), **({"reason": "cost_exceeded_before_call"} if paused else {})},
        )


class DualReviewPlugin:
    """双模型复核钩子：single 由引擎跳过；dual 时空实现；禁止同模双跑。"""

    name = "dual_review"
    uses_model = True

    def run(self, ctx: StepContext) -> StepResult:
        from backend.jobs.codes import CAT_INCOMPLETE, ERR_SAME_MODEL_DUAL
        from backend.models.presets import same_base_model

        if ctx.model_status != "dual":
            return StepResult(state="skipped", detail={"reason": "model_status_not_dual"})

        primary = ctx.primary_model or {}
        review = ctx.review_model or {}
        if same_base_model(
            primary.get("provider", ""),
            primary.get("model", ""),
            primary.get("base_url"),
            review.get("provider", ""),
            review.get("model", ""),
            review.get("base_url"),
        ):
            return StepResult(
                state="failed",
                incomplete=[{
                    "unit_id": None,
                    "reason": "主模型与复核模型相同，已禁止同模双跑",
                    "category": CAT_INCOMPLETE,
                }],
                detail={"code": ERR_SAME_MODEL_DUAL},
            )

        # 空钩子：DC-13 挂入；本单 0 次复核调用
        hook = ctx.extras.get("dual_review_hook")
        if callable(hook):
            if ctx.allow_model_call is not None and not ctx.allow_model_call():
                return StepResult(state="failed", detail={"reason": "cost_exceeded_before_call"})
            return hook(ctx)
        from backend.review import ReviewService
        service = ReviewService(ctx.store, task_id=ctx.task_id,
            primary_model=primary, review_model=review,
            client=ctx.extras.get("review_client"),
            client_factory=ctx.extras.get("review_client_factory"),
            allow_model_call=ctx.allow_model_call, progress_callback=ctx.extras.get("progress_callback"))
        result = service.run(ctx.version_id)
        return StepResult(state="failed" if result.cost_paused else ("failed" if result.incomplete else "done"),
            incomplete=result.incomplete, model_calls=result.model_calls,
            review_model_calls=result.model_calls, estimated_cost_micros=result.estimated_cost_micros,
            detail={"reason": "cost_exceeded_before_call" if result.cost_paused else "reviewed", "reviewed": result.reviewed, "cache_hits": result.cache_hits})


class FinalizePlugin:
    name = "finalize"
    uses_model = False

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult(state="done", detail={"finalized": True})


class SemanticPlugin:
    name = "semantic"
    # Check the cost gate immediately before each cache miss, not before local work.
    uses_model = False

    def run(self, ctx: StepContext) -> StepResult:
        factory = ctx.extras.get("semantic_client_factory")
        if not ctx.extras.get("semantic_disclosed") and factory is None:
            return StepResult("skipped", detail={"reason": "semantic_disclosure_required"})
        if not ctx.primary_model or (ctx.extract_factory is not None and factory is None):
            return StepResult("skipped", detail={"reason": "semantic_client_unavailable"})
        from backend.extract.semantic_job import run_semantic_text

        result = run_semantic_text(ctx.store, ctx.version_id, ctx.task_id, route=ctx.primary_model,
            client_factory=factory or ctx.extras["primary_client_factory"], allow_model_call=ctx.allow_model_call,
            report=ctx.extras.get("progress_callback"), cancelled=ctx.extras.get("cancel_event"))
        detail = {"cache_hits": result["cache_hits"], "coverage": result.get("diagnostics", [])}
        if result.get("cost_paused"):
            detail["reason"] = "cost_exceeded_before_call"
        return StepResult("failed" if result.get("cost_paused") or result.get("interrupted") else "done",
                          model_calls=result["calls"], estimated_cost_micros=result["cost"],
                          incomplete=result["incomplete"], detail=detail)


class LinkPlugin:
    """DC-11 自动关联；uses_model=False。"""

    name = "link"
    uses_model = False

    def run(self, ctx: StepContext) -> StepResult:
        from backend.linking import LinkingService

        svc = LinkingService(ctx.store)
        result = svc.link_version(ctx.version_id, persist=True)
        return StepResult(
            state="done",
            incomplete=list(result.incomplete),
            detail={
                "association_count": len(result.associations),
                "same_name_pending": len(result.same_name_pending),
                "rejected_name_only": result.rejected_name_only,
                "rejected_digit_only": result.rejected_digit_only,
            },
        )


class RulesAPlugin:
    """DC-11 规则 A（目录图签/尺寸/标高）；数值本地比较，uses_model=False。"""

    name = "rules_a"
    uses_model = False

    def run(self, ctx: StepContext) -> StepResult:
        from backend.rules import RulesAService

        svc = RulesAService(ctx.store)
        result = svc.run(
            ctx.version_id,
            task_id=ctx.task_id,
            settings=ctx.settings,
            persist=True,
        )
        return StepResult(
            state="done",
            incomplete=list(result.incomplete),
            detail={
                "issue_count": len(result.issues),
                "pending_count": len(result.pendings),
                **result.detail,
            },
        )


class RulesBPlugin:
    name = "rules_b"
    uses_model = False

    def run(self, ctx):
        from backend.rules import RulesBService
        result = RulesBService(ctx.store).run(ctx.version_id)
        return StepResult(state="done", incomplete=result.incomplete,
            detail={"issue_count": len(result.issues), "pending_count": len(result.pendings), **result.detail})


def get_default_registry() -> StepRegistry:
    reg = StepRegistry()
    reg.register(ExtractPlugin())
    reg.register(SemanticPlugin())
    reg.register(LinkPlugin())
    reg.register(RulesAPlugin())
    reg.register(RulesBPlugin())
    reg.register(DualReviewPlugin())
    reg.register(FinalizePlugin())
    return reg
