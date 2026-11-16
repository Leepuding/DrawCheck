from datetime import datetime, timedelta, timezone

import pytest

from backend.jobs import JobService, StepRegistry, StepResult
from backend.jobs.plugins import NoOpPlugin
from backend.jobs.codes import STEPS
from tests.backend.jobs.conftest import configure_single_model


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def registry(extract):
    result = StepRegistry()
    for name in STEPS:
        result.register(NoOpPlugin(name))
    result.register(extract)
    return result


@pytest.mark.parametrize("fail", [False, True])
def test_review_observed_units_region_progress_and_terminal_freeze(repo, settings_svc, drawing, fail):
    configure_single_model(settings_svc)
    clock, snapshots = Clock(), []
    class Extract:
        name, uses_model = "extract", False
        def run(self, ctx):
            report = ctx.extras["progress_callback"]
            region = ctx.extras["region_progress_callback"]
            report(0, 3, unit="张图")
            initial = JobService(repo, clock=clock).get_progress(ctx.task_id)
            assert initial["eta_seconds"] is None
            clock.advance(2)
            report(1, 3)
            snapshots.append(JobService(repo, clock=clock).get_progress(ctx.task_id))
            clock.advance(2)
            report(2, 3)
            region(1, 4)
            snapshots.append(JobService(repo, clock=clock).get_progress(ctx.task_id))
            if fail:
                return StepResult("failed", incomplete=[{"reason":"controlled failure","category":"incomplete"}])
            clock.advance(2)
            report(3, 3)
            return StepResult("done")
    jobs = JobService(repo, clock=clock, settings_service=settings_svc, registry=registry(Extract()))
    final = jobs.start_review(drawing["version"]["id"])
    assert snapshots[0]["eta_seconds"] is None
    assert snapshots[1]["eta_seconds"] == 2
    assert snapshots[1]["phase_key"] == "recognizing"
    assert snapshots[1]["processed_count"] == 2 and snapshots[1]["total_count"] == 3
    assert 0 < snapshots[0]["percent"] < snapshots[1]["percent"] < 17
    assert snapshots[1]["region_percent"] == 25
    assert snapshots[1]["region_processed_count"] == 1 and snapshots[1]["region_total_count"] == 4
    clock.advance(100)
    later = jobs.get_progress(final["task_id"])
    assert later["elapsed_seconds"] == final["elapsed_seconds"] == (4 if fail else 6)
    assert later["eta_seconds"] is None
    assert final["status"] == ("失败" if fail else "已完成")
    if fail:
        assert final["percent"] == snapshots[1]["percent"]
        assert final["phase_percent"] < 100
    else:
        assert final["percent"] == 100 and final["region_total_count"] == 0


def test_review_queue_and_pause_time_are_excluded_on_resume(repo, settings_svc, drawing):
    configure_single_model(settings_svc)
    clock = Clock()
    class Extract:
        name, uses_model, calls = "extract", False, 0
        def run(self, ctx):
            self.calls += 1
            ctx.extras["progress_callback"](0, 3)
            clock.advance(2)
            ctx.extras["progress_callback"](1, 3)
            if self.calls == 1:
                return StepResult("failed", detail={"reason":"cost_exceeded_before_call"})
            clock.advance(2)
            ctx.extras["progress_callback"](3, 3)
            return StepResult("done")
    jobs = JobService(repo, clock=clock, settings_service=settings_svc, registry=registry(Extract()))
    queued = jobs.start_review(drawing["version"]["id"], run=False)
    clock.advance(50)
    assert jobs.get_progress(queued["task_id"])["elapsed_seconds"] == 0
    first = jobs.start_review(drawing["version"]["id"])
    assert first["status"] == "暂停" and first["elapsed_seconds"] == 2
    clock.advance(100)
    assert jobs.get_progress(first["task_id"])["elapsed_seconds"] == 2
    second = jobs.start_review(drawing["version"]["id"])
    assert second["task_id"] == first["task_id"] and second["status"] == "已完成"
    assert second["elapsed_seconds"] == 6


def test_extract_plugin_reports_actual_completed_drawings_and_resets_regions(repo, settings_svc, drawing):
    configure_single_model(settings_svc)
    version_id = drawing["version"]["id"]
    for ordinal in (2, 3):
        repo.create_unit(version_id, drawing["original"]["id"], kind="pdf_page", ordinal=ordinal, name=str(ordinal))
    clock, snapshots = Clock(), []
    class Extract:
        def extract_unit(self, unit_id, **kwargs):
            self.region_progress_callback(0, 4)
            snapshot = JobService(repo, clock=clock).get_progress(task_id[0])
            snapshots.append(snapshot)
            clock.advance(2)
            self.region_progress_callback(4, 4)
            from types import SimpleNamespace
            return SimpleNamespace(unit_id=unit_id, status="ok", model_calls=0)
    jobs = JobService(repo, clock=clock, settings_service=settings_svc, extract_factory=Extract)
    pending = jobs.start_review(version_id, run=False)
    task_id = [pending["task_id"]]
    jobs.start_review(version_id)
    assert [s["processed_count"] for s in snapshots] == [0, 1, 2]
    assert all(s["total_count"] == 3 for s in snapshots)
    assert all(s["region_processed_count"] == 0 and s["region_total_count"] == 4 for s in snapshots)
    assert snapshots[0]["eta_seconds"] is None and snapshots[2]["eta_seconds"] == 2
