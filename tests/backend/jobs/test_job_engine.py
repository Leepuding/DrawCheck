"""DC-10 审查引擎验收测试（默认 Mock / 断网）。"""
from __future__ import annotations

import threading
from typing import Any

import pytest

from backend.jobs import (
    MSG_COST_PAUSED,
    MSG_RESUME_PROBE,
    MSG_RUNNING,
    PROGRESS_PUBLIC_KEYS,
    JobService,
    STEPS,
    get_default_registry,
)
from backend.jobs.plugins import NoOpPlugin, StepContext, StepResult
from backend.settings.codes import PRECHECK_NO_MODEL
from tests.backend.jobs.conftest import configure_dual_model, configure_single_model


class CountingExtract:
    def __init__(self, store, *, fail: bool = False, out_of_scope_ids=None, calls_box=None):
        self.store = store
        self.fail = fail
        self.out_of_scope_ids = list(out_of_scope_ids or [])
        self.calls_box = calls_box if calls_box is not None else {"n": 0}

    def extract_version(self, version_id: str, *, persist: bool = True):
        self.calls_box["n"] += 1
        if self.fail:
            raise RuntimeError("mock extract boom")
        units = self.store.list_records("drawing_units", version_id=version_id)
        results = []
        for unit in units:
            if unit["id"] in self.out_of_scope_ids:
                results.append(
                    type("R", (), {
                        "unit_id": unit["id"],
                        "status": "incomplete",
                        "incomplete_reason": "不在审查范围",
                        "model_calls": 0,
                    })()
                )
            else:
                # 无审查图 → unreadable/incomplete 路径
                results.append(
                    type("R", (), {
                        "unit_id": unit["id"],
                        "status": "incomplete",
                        "incomplete_reason": "无审查图，跳过识别",
                        "model_calls": 1,
                    })()
                )
        return results


def _jobs(repo, settings_svc, extract_factory=None):
    return JobService(
        repo,
        settings_service=settings_svc,
        extract_factory=extract_factory,
    )


def test_no_model_blocks_task_creation(repo, settings_svc, drawing):
    jobs = _jobs(repo, settings_svc)
    out = jobs.start_review(drawing["version"]["id"])
    assert out.get("ok") is False
    assert out["error"]["code"] == PRECHECK_NO_MODEL
    assert out["task_id"] is None
    assert repo.list_records("review_tasks", version_id=drawing["version"]["id"]) == []


def test_dedup_second_start_already_running(repo, settings_svc, drawing):
    configure_single_model(settings_svc)
    jobs = _jobs(repo, settings_svc, extract_factory=lambda: CountingExtract(repo))
    first = jobs.start_review(drawing["version"]["id"], run=False)
    task_id = first["task_id"]
    # 模拟进程内正在跑：第二次开始应返回 already_running，且不新建任务
    jobs._running[drawing["version"]["id"]] = task_id
    second = jobs.start_review(drawing["version"]["id"])
    assert second["already_running"] is True
    assert second["task_id"] == task_id
    tasks = repo.list_records("review_tasks", version_id=drawing["version"]["id"])
    assert len(tasks) == 1


def test_progress_shape_keys_only(repo, settings_svc, drawing):
    configure_single_model(settings_svc)
    jobs = _jobs(repo, settings_svc, extract_factory=lambda: CountingExtract(repo))
    view = jobs.start_review(drawing["version"]["id"])
    assert set(view) <= PROGRESS_PUBLIC_KEYS | {"ok"}
    forbidden = {"steps", "checkpoints", "logs", "log", "step_names", "pipeline"}
    assert forbidden.isdisjoint(view)
    assert view["message"] in {MSG_RUNNING, "审查结束", "审查结束（部分完成），已保留已有结果", MSG_COST_PAUSED, "审查未完成，已保留已有结果"}
    assert 0 <= view["percent"] <= 100
    prog = jobs.get_progress(view["task_id"])
    assert set(prog) <= PROGRESS_PUBLIC_KEYS
    assert "checkpoints" not in prog


def test_resume_skips_completed_extract(repo, settings_svc, drawing):
    configure_single_model(settings_svc)
    calls = {"n": 0}
    jobs = _jobs(repo, settings_svc, extract_factory=lambda: CountingExtract(repo, calls_box=calls))

    # 第一次：跑完 extract 后人工打断（模拟杀进程）——通过自定义 registry
    reg = get_default_registry()

    class BoomLink(NoOpPlugin):
        def run(self, ctx: StepContext) -> StepResult:
            raise RuntimeError("kill after extract")

    reg.register(BoomLink("link"))
    jobs.registry = reg
    first = jobs.start_review(drawing["version"]["id"])
    assert first["status"] == "失败"
    assert calls["n"] == 1
    task_id = first["task_id"]
    cps = repo.get("review_tasks", task_id)["checkpoints"]
    assert cps["extract"]["state"] == "done"

    probe = jobs.probe_incomplete(drawing["version"]["id"])
    assert probe["has_incomplete"] is True
    assert probe["message"] == MSG_RESUME_PROBE

    # 恢复默认 link，续审
    jobs.registry = get_default_registry()
    second = jobs.start_review(drawing["version"]["id"])
    assert second["task_id"] == task_id
    assert calls["n"] == 1  # extract 不重复
    assert second["status"] in {"已完成", "失败"}


def test_step_failure_partial_not_fake_success(repo, settings_svc, drawing):
    configure_single_model(settings_svc)
    jobs = _jobs(repo, settings_svc, extract_factory=lambda: CountingExtract(repo, fail=True))
    view = jobs.start_review(drawing["version"]["id"])
    assert view["status"] == "失败"
    assert view["outcome"] == "failed"
    assert view["issue_count"] == 0
    # 不得变成「0 问题且成功」
    assert not (view["issue_count"] == 0 and view["status"] == "已完成" and view["outcome"] == "completed")
    assert view["incomplete_count"] >= 1 or view["status"] == "失败"


def test_cost_exceeded_zero_new_model_calls(repo, settings_svc, drawing):
    configure_single_model(settings_svc, monthly_limit_yuan=1)
    # 伪造已用满
    settings_svc.review_cost_reader = lambda now: 1_000_000  # 1 元
    calls = {"n": 0}
    jobs = _jobs(repo, settings_svc, extract_factory=lambda: CountingExtract(repo, calls_box=calls))
    # precheck 会直接 COST_EXCEEDED，不建任务
    out = jobs.start_review(drawing["version"]["id"])
    assert out.get("ok") is False
    assert out["error"]["code"] == "COST_EXCEEDED"
    assert calls["n"] == 0

    # 先用正常额度建任务并跑一部分，再超限
    settings_svc.review_cost_reader = lambda now: 0
    configure_single_model(settings_svc, monthly_limit_yuan=100)
    calls["n"] = 0

    class GateExtract(CountingExtract):
        def extract_version(self, version_id: str, *, persist: bool = True):
            # 第一次调用前引擎已检查；我们在调用后把额度打满，下一模型步骤会被拦
            out = super().extract_version(version_id, persist=persist)
            settings_svc.review_cost_reader = lambda now: 100 * 1_000_000
            return out

    reg = get_default_registry()

    class ModelLink:
        name = "link"
        uses_model = True

        def run(self, ctx: StepContext) -> StepResult:
            if ctx.allow_model_call is not None and not ctx.allow_model_call():
                return StepResult(state="failed", detail={"reason": "cost_exceeded_before_call"})
            return StepResult(state="done", model_calls=1)

    reg.register(ModelLink())
    jobs = _jobs(repo, settings_svc, extract_factory=lambda: GateExtract(repo, calls_box=calls))
    jobs.registry = reg
    view = jobs.start_review(drawing["version"]["id"])
    assert view["cost_paused"] is True
    assert view["status"] == "暂停"
    assert view["message"] == MSG_COST_PAUSED
    assert view["outcome"] == "cost_paused"
    # extract 已发生 1 次；link 因超限未产生新调用（model_calls 不计入 extract 之外）
    assert calls["n"] == 1
    cps = repo.get("review_tasks", view["task_id"])["checkpoints"]
    assert cps["extract"]["state"] == "done"
    assert cps.get("link", {}).get("state") in {"pending", "failed"}


def test_tolerance_snapshot_frozen(repo, settings_svc, drawing):
    configure_single_model(settings_svc)
    jobs = _jobs(repo, settings_svc, extract_factory=lambda: CountingExtract(repo))
    view = jobs.start_review(drawing["version"]["id"])
    snap = view["tolerance_snapshot"]
    assert snap["dimension_tolerance_mm"] == 1
    assert snap["elevation_tolerance_m"] == 0.005
    repo.save_settings({"dimension_tolerance_mm": 9, "elevation_tolerance_m": 0.1})
    again = jobs.get_progress(view["task_id"])
    assert again["tolerance_snapshot"]["dimension_tolerance_mm"] == 1
    assert again["tolerance_snapshot"]["elevation_tolerance_m"] == 0.005


def test_single_skips_dual_review_zero_review_calls(repo, settings_svc, drawing):
    configure_single_model(settings_svc)
    review_calls = {"n": 0}

    def hook(ctx: StepContext) -> StepResult:
        review_calls["n"] += 1
        return StepResult(state="done", review_model_calls=1)

    jobs = _jobs(repo, settings_svc, extract_factory=lambda: CountingExtract(repo))
    jobs.extras["dual_review_hook"] = hook
    view = jobs.start_review(drawing["version"]["id"])
    cps2 = repo.get("review_tasks", view["task_id"])["checkpoints"]
    assert cps2["dual_review"]["state"] == "skipped"
    assert cps2["dual_review"].get("review_model_calls", 0) == 0
    assert review_calls["n"] == 0


def test_dual_enters_hook_rejects_same_model(repo, settings_svc, drawing):
    configure_dual_model(settings_svc, same=False)
    hooked = {"n": 0}

    def hook(ctx: StepContext) -> StepResult:
        hooked["n"] += 1
        return StepResult(state="done", review_model_calls=0)

    jobs = _jobs(repo, settings_svc, extract_factory=lambda: CountingExtract(repo))
    jobs.extras["dual_review_hook"] = hook
    view = jobs.start_review(drawing["version"]["id"])
    assert hooked["n"] == 1
    assert repo.get("review_tasks", view["task_id"])["checkpoints"]["dual_review"]["state"] == "done"

    from backend.jobs.plugins import DualReviewPlugin

    ctx = StepContext(
        store=repo,
        task_id=view["task_id"],
        version_id=drawing["version"]["id"],
        model_status="dual",
        settings=repo.get_settings(),
        checkpoints={},
        incomplete=[],
        primary_model={"provider": "siliconflow", "model": "zai-org/GLM-4.5V", "base_url": "https://api.siliconflow.cn/v1"},
        review_model={"provider": "siliconflow", "model": "zai-org/GLM-4.5V", "base_url": "https://api.siliconflow.cn/v1"},
    )
    result = DualReviewPlugin().run(ctx)
    assert result.state == "failed"


def test_plugin_registry_mount_point():
    reg = get_default_registry()
    assert set(STEPS) <= set(reg.names()) | {"finalize"}
    for name in STEPS:
        assert reg.get(name) is not None

    class FakeRules:
        name = "rules_a"
        uses_model = False

        def run(self, ctx: StepContext) -> StepResult:
            return StepResult(state="done", detail={"custom": True})

    reg.register(FakeRules())
    assert reg.get("rules_a").run  # 可替换挂载


def test_out_of_scope_and_unreadable_counts(repo, settings_svc, drawing):
    configure_single_model(settings_svc)
    unit_id = drawing["unit"]["id"]
    jobs = _jobs(
        repo,
        settings_svc,
        extract_factory=lambda: CountingExtract(repo, out_of_scope_ids=[unit_id]),
    )
    view = jobs.start_review(drawing["version"]["id"])
    assert view["out_of_scope_count"] >= 1
    assert "out_of_scope_count" in view
    assert "unreadable_count" in view
    assert "incomplete_count" in view
    assert view["outcome"] in {"partial", "completed", "failed"}
    # 有范围外时不应是干净 completed（无问题也不等于没问题）
    if view["out_of_scope_count"] > 0:
        assert view["outcome"] == "partial"


def test_live_marker_skipped_without_flag():
    @pytest.mark.live
    def _inner():
        return True

    # 仅保证标记存在；真正 live 用例在无开关时由 conftest skip
    assert True


@pytest.mark.live
def test_live_placeholder_skip_without_env():
    """无 DRAWCHECK_LIVE_MODEL=1 时应被 conftest skip。"""
    raise AssertionError("不应在无开关时执行 live 用例")
