"""DC-11 规则 A 对照 answers.json；含容差可配、缺单位 pending、字段形状、W7-Q2=B。"""
from __future__ import annotations

import pytest

from backend.jobs import StepContext, get_default_registry
from backend.linking import LinkingService
from backend.linking.service import SAME_NAME_PENDING_REASON
from backend.rules import RulesAService


def _fixture_ids(repo, vid):
    return {
        i["rule_result"].get("fixture_id")
        for i in repo.list_records("issues", version_id=vid)
        if i["rule_result"].get("fixture_id")
    }


def _issue_cats(repo, vid):
    return [i["rule_result"].get("category") for i in repo.list_records("issues", version_id=vid)]


def test_rule_group_a_should_and_should_not(repo, seeded_rule_a):
    seeded = seeded_rule_a
    vid = seeded["version"]["id"]
    task_id = seeded["task"]["id"]

    LinkingService(repo).link_version(vid)
    RulesAService(repo).run(vid, task_id=task_id, persist=True)

    issues = repo.list_records("issues", version_id=vid)
    pendings = repo.list_records("pending_confirmations", version_id=vid)
    fids = _fixture_ids(repo, vid)
    cats = _issue_cats(repo, vid)

    assert "ISSUE-CAT-MISSING" in fids
    assert "ISSUE-CAT-EXTRA" in fids
    assert "ISSUE-CAT-COUNT" in fids
    assert "ISSUE-PREFIX-BAD" in fids
    assert "ISSUE-DIM-CONFLICT" in fids
    assert "ISSUE-DIM-TOL-OVER" in fids
    assert "ISSUE-ELV-CONFLICT" in fids
    assert "ISSUE-ELV-TOL-OVER" in fids

    for i in issues:
        rr = i["rule_result"]
        if rr.get("category") == "尺寸" and rr.get("left_value") == "200×200mm":
            pytest.fail("ISSUE-DIM-OK 误报")
        if rr.get("category") == "尺寸" and {rr.get("left_value"), rr.get("right_value")} == {"100mm", "101mm"}:
            pytest.fail("ISSUE-DIM-TOL-EQ 误报")
        if rr.get("category") == "标高" and rr.get("left_value") == "3.000m" and rr.get("right_value") == "3.000m":
            pytest.fail("ISSUE-ELV-OK 误报")
        if rr.get("category") == "标高" and {rr.get("left_value"), rr.get("right_value")} == {"4.350m", "4.355m"}:
            pytest.fail("ISSUE-ELV-TOL-EQ 误报")
        if "S-001" in i["title"] and "缺图" in i["title"]:
            pytest.fail("ISSUE-CAT-OK 误报")
        if rr.get("fixture_id") == "ISSUE-PREFIX-BAD" and (rr.get("left_value") or "").startswith("S-001"):
            pytest.fail("ISSUE-PREFIX-OK 误报")

    assert "目录与图签不符" in cats
    assert "尺寸" in cats
    assert "标高" in cats
    assert "图签编号前缀" in cats

    assert "ISSUE-DIM-NO-UNIT" not in fids
    assert not any("单位" in (p.get("reason") or "") for p in pendings)

    # W7-Q2=B：ISSUE-SAME-NAME → 0 疑似 + 1 待确认；SAME-NAME-OK 双零
    same_name_issues = [
        i for i in issues
        if i.get("left_text") == "基础F1" or "同名" in (i.get("title") or "")
    ]
    assert same_name_issues == []
    same_pending = [p for p in pendings if p.get("text") == "基础F1"]
    assert len(same_pending) == 1
    assert same_pending[0]["reason"] == SAME_NAME_PENDING_REASON
    assert not any(p.get("text") == "支架Z1" for p in pendings)


def test_dim_conflict_has_both_bboxes(repo, seeded_rule_a):
    seeded = seeded_rule_a
    vid = seeded["version"]["id"]
    RulesAService(repo).run(vid, task_id=seeded["task"]["id"])
    conflicts = [
        i for i in repo.list_records("issues", version_id=vid)
        if i["rule_result"].get("fixture_id") == "ISSUE-DIM-CONFLICT"
    ]
    assert conflicts
    issue = conflicts[0]
    for prefix in ("left_", "right_"):
        for k in ("x0", "y0", "x1", "y1"):
            v = issue[f"{prefix}{k}"]
            assert 0.0 <= v <= 1.0
    assert issue["review_status"] == "疑似"
    assert issue["title"]
    assert issue["rule_result"].get("basis")
    assert issue["rule_result"].get("left_value")
    assert issue["rule_result"].get("right_value")


def test_tolerance_configurable_from_snapshot(repo, seed_rule_a):
    seeded = seed_rule_a(dim_tol=1.0, elv_tol=0.005)
    vid = seeded["version"]["id"]
    RulesAService(repo).run(vid, task_id=seeded["task"]["id"])
    over_default = [
        i for i in repo.list_records("issues", version_id=vid)
        if i["rule_result"].get("fixture_id") == "ISSUE-DIM-TOL-OVER"
    ]
    assert over_default

    seeded2 = seed_rule_a(dim_tol=2.0, elv_tol=0.005)
    vid2 = seeded2["version"]["id"]
    assert seeded2["task"]["tolerance_snapshot"]["dimension_tolerance_mm"] == 2.0
    RulesAService(repo).run(vid2, task_id=seeded2["task"]["id"])
    overs = [
        i for i in repo.list_records("issues", version_id=vid2)
        if i["rule_result"].get("fixture_id") == "ISSUE-DIM-TOL-OVER"
    ]
    assert overs == []


def test_elevation_tolerance_boundary(repo, seed_rule_a):
    seeded = seed_rule_a(elv_tol=0.005)
    vid = seeded["version"]["id"]
    RulesAService(repo).run(vid, task_id=seeded["task"]["id"])
    for i in repo.list_records("issues", version_id=vid):
        rr = i["rule_result"]
        if {rr.get("left_value"), rr.get("right_value")} == {"4.350m", "4.355m"}:
            pytest.fail("等于容差不应报")


def test_issue_fields_a7(repo, seeded_rule_a):
    seeded = seeded_rule_a
    vid = seeded["version"]["id"]
    RulesAService(repo).run(vid, task_id=seeded["task"]["id"])
    for issue in repo.list_records("issues", version_id=vid):
        assert issue["title"]
        assert issue["left_unit_id"]
        assert issue["right_unit_id"]
        assert issue["review_status"] == "疑似"
        rr = issue["rule_result"]
        assert rr.get("basis")
        assert "category" in rr
        assert "left_value" in rr
        assert "right_value" in rr


def test_plugins_registered_uses_model_false(repo, seeded_rule_a):
    reg = get_default_registry()
    link = reg.get("link")
    rules = reg.get("rules_a")
    assert link is not None and rules is not None
    assert link.uses_model is False
    assert rules.uses_model is False

    seeded = seeded_rule_a
    vid = seeded["version"]["id"]
    task = seeded["task"]
    settings = repo.get_settings()
    ctx = StepContext(
        store=repo,
        task_id=task["id"],
        version_id=vid,
        model_status="single",
        settings=settings,
        checkpoints={},
        incomplete=[],
        primary_model=settings.get("primary_model"),
        review_model=None,
    )
    link_res = link.run(ctx)
    rules_res = rules.run(ctx)
    assert link_res.state == "done"
    assert rules_res.state == "done"
    assert link_res.model_calls == 0
    assert rules_res.model_calls == 0
    assert repo.list_records("issues", version_id=vid)
    assert any(
        p["reason"] == SAME_NAME_PENDING_REASON
        for p in repo.list_records("pending_confirmations", version_id=vid)
    )


def test_job_pipeline_link_rules_not_noop():
    reg = get_default_registry()
    assert type(reg.get("link")).__name__ == "LinkPlugin"
    assert type(reg.get("rules_a")).__name__ == "RulesAPlugin"
    assert type(reg.get("rules_b")).__name__ == "RulesBPlugin"
