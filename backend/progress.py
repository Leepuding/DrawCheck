"""Public phase progress based on observed work; internal timestamps never leave the service."""
from __future__ import annotations

import math

PROGRESS_FIELDS = frozenset({"phase_key", "phase", "processed_count", "total_count", "progress_unit",
    "phase_percent", "elapsed_seconds", "eta_seconds", "eta_scope", "estimate_basis"})


def new_progress():
    return {"elapsed":0.0, "active_since":None, "phase_started_elapsed":0.0,
        "phase_key":"queued", "phase":"等待处理", "processed_count":0, "total_count":0,
        "progress_unit":"", "sample_elapsed":0.0}


def elapsed(state, now, *, live):
    value = float(state.get("elapsed", 0))
    since = state.get("active_since")
    if live and since is not None:
        value += max(0.0, float(now)-float(since))
    return max(0.0, value)


def tick(state, now, *, running):
    state["elapsed"] = elapsed(state, now, live=True)
    state["active_since"] = float(now) if running else None


def restart(state, now):
    # The persisted elapsed value is authoritative. Never charge queued, paused,
    # or process-down time between the last checkpoint and this new attempt.
    state["active_since"] = float(now)


def begin_phase(state, now, key, label, total=0, unit="个步骤"):
    tick(state, now, running=True)
    state.update(phase_key=key, phase=label, processed_count=0, total_count=max(0,int(total)),
        progress_unit=unit, phase_started_elapsed=state["elapsed"], sample_elapsed=0.0)


def update_count(state, now, processed, total=None):
    tick(state, now, running=True)
    if total is not None:
        state["total_count"] = max(0, int(total))
    previous = state.get("processed_count", 0)
    state["processed_count"] = max(0, min(int(processed), state["total_count"]))
    if state["processed_count"] > previous:
        state["sample_elapsed"] = max(0.0, state["elapsed"]-state.get("phase_started_elapsed", 0))


def view_progress(state, now, *, status, live):
    running = status in {"running", "进行中"}
    queued = status in {"queued", "待开始"}
    terminal = {"completed":("completed", "导入已结束"), "已完成":("completed", "审查已结束"),
        "failed":("failed", "导入中断"), "失败":("failed", "审查中断"),
        "rejected":("rejected", "未导入"), "暂停":("paused", "审查已暂停")}.get(status)
    spent = 0.0 if queued else elapsed(state, now, live=live and running)
    done, total = state.get("processed_count", 0), state.get("total_count", 0)
    phase_percent = round(done/total*100, 1) if total else None
    if terminal and terminal[0] != "completed" and phase_percent is not None:
        phase_percent = min(99.0, phase_percent)
    eta = None
    basis = "尚无足够的当前阶段处理记录，暂无法估计"
    sampled = state.get("sample_elapsed", 0)
    if running and live and done >= 2 and total > done and sampled >= 0.5:
        phase_elapsed = max(0, spent-state.get("phase_started_elapsed", 0))
        remaining = sampled/done*total-phase_elapsed
        if math.isfinite(remaining) and remaining > 0:
            eta = round(remaining, 1)
            basis = f"按当前阶段已完成 {done} 项的实际平均耗时估算；不包含后续阶段"
        else:
            basis = "当前处理已超出已观测平均耗时，暂无法估计剩余时间"
    elif terminal or queued:
        basis = "当前未在处理，不估计剩余时间"
    key, label = terminal or (state.get("phase_key", "queued"), state.get("phase", "等待处理"))
    return {"phase_key":key, "phase":label, "processed_count":done, "total_count":total,
        "progress_unit":state.get("progress_unit", ""), "phase_percent":phase_percent,
        "elapsed_seconds":round(spent, 1), "eta_seconds":eta, "eta_scope":"phase", "estimate_basis":basis}
