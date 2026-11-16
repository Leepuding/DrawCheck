from backend.progress import new_progress, restart, begin_phase, update_count, tick, view_progress


def test_eta_needs_observations_and_reverts_to_unknown_when_overdue():
    state = new_progress()
    restart(state, 0)
    begin_phase(state, 0, "recognizing", "正在识别图纸", 3, "张图")
    assert view_progress(state, 1, status="进行中", live=True)["eta_seconds"] is None
    update_count(state, 2, 1)
    assert view_progress(state, 2, status="进行中", live=True)["eta_seconds"] is None
    update_count(state, 4, 2)
    assert view_progress(state, 4, status="进行中", live=True)["eta_seconds"] == 2
    overdue = view_progress(state, 20, status="进行中", live=True)
    assert overdue["eta_seconds"] is None and "超出" in overdue["estimate_basis"]
    begin_phase(state, 20, "second_review", "正在复核", 5, "条问题")
    assert view_progress(state, 20, status="进行中", live=True)["eta_seconds"] is None


def test_inactive_process_does_not_count_unsampled_downtime():
    state = new_progress()
    restart(state, 0)
    begin_phase(state, 0, "recognizing", "正在识别图纸", 3, "张图")
    update_count(state, 4, 2)
    inactive = view_progress(state, 1000, status="进行中", live=False)
    assert inactive["elapsed_seconds"] == 4 and inactive["eta_seconds"] is None
    restart(state, 1000)
    tick(state, 1002, running=False)
    assert view_progress(state, 3000, status="暂停", live=False)["elapsed_seconds"] == 6
