"""模型字段、跨版本引用、历史保护、断点记录与设置持久化。"""
import sqlite3

import pytest

from backend.store import BBox, ErrorCode, Evidence, Repository, Source, StoreError
from conftest import issue


def test_fact_issue_pending_round_trip_with_provenance(repo, drawing):
    vid, evidence = drawing["version"]["id"], drawing["evidence"]
    fact = repo.add_fact(vid, evidence, numeric_value="2400.000", unit="毫米")
    pending = repo.create_pending(vid, evidence, "两模型读数不一致")
    problem = issue(repo, drawing)
    for record in (fact, pending):
        assert record["source"] == "PDF文字"
        assert record["unit_id"] == drawing["unit"]["id"]
        assert [record[key] for key in ("x0", "y0", "x1", "y1")] == [0.1, 0.2, 0.3, 0.4]
    assert fact["text"] == "尺寸 2400" and fact["numeric_value"] == "2400.000" and fact["unit"] == "毫米"
    for prefix in ("left_", "right_"):
        assert problem[f"{prefix}source"] == "PDF文字"
        assert problem[f"{prefix}unit_id"] == evidence.unit_id
        assert problem[f"{prefix}x0"] == 0.1 and problem[f"{prefix}y1"] == 0.4
    assert problem["rule_result"]["matched"] is False
    assert problem["primary_opinion"]["model"] == "模拟主模型"
    assert problem["review_opinion"]["model"] == "模拟复核模型"
    assert repo.confirm_pending(pending["id"], status="已确认", confirmed_text="2410")["confirmed_text"] == "2410"
    assert repo.update_issue(problem["id"], review_status="排除", review_note="人工核对")["review_note"] == "人工核对"
    assert repo.update_issue(problem["id"], review_status="疑似")["review_status"] == "疑似"


def test_names_and_persistence_after_reopen(repo, drawing):
    root = repo.root
    with Repository(root) as reopened:
        for table, key in (("projects", "project"), ("drawing_versions", "version"),
                            ("original_files", "original"), ("drawing_units", "unit")):
            assert reopened.get(table, drawing[key]["id"])["name"] == drawing[key]["name"]
        assert reopened.update_unit_directory(drawing["unit"]["id"], name="中文 空格 T0105(二）", drawing_number="图 01")["name"] == "中文 空格 T0105(二）"


def test_issue_evidence_can_reference_two_distinct_units(repo, drawing):
    second = repo.create_unit(drawing["version"]["id"], drawing["original"]["id"], kind="pdf_page", ordinal=2, name="第二页")
    right = Evidence(second["id"], Source.VISION, "2410", BBox(0.7, 0.8, 1, 1))
    row = repo.create_issue(drawing["version"]["id"], "两图尺寸", drawing["evidence"], right, rule_result={})
    assert row["left_unit_id"] != row["right_unit_id"]
    assert row["right_x0"] == 0.7 and row["right_source"] == "看图识别"


def test_version_history_readonly_and_other_project_continues(repo, drawing):
    problem = issue(repo, drawing)
    old_id = drawing["version"]["id"]
    newer = repo.create_version(drawing["project"]["id"], "修订 B")
    with pytest.raises(StoreError) as caught:
        repo.update_issue(problem["id"], review_status="已确认")
    assert caught.value.code == ErrorCode.VERSION_READONLY
    assert repo.get("issues", problem["id"]) == problem
    for sql in ("DELETE FROM issues WHERE id=?", "UPDATE issues SET title='改写' WHERE id=?",
                "UPDATE issues SET version_id='不存在' WHERE id=?"):
        with pytest.raises(sqlite3.IntegrityError, match="VERSION_READONLY"):
            repo._connection.execute(sql, (problem["id"],))
    assert [v["sequence"] for v in repo.list_records("drawing_versions", project_id=drawing["project"]["id"])] == [1, 2]
    with pytest.raises(StoreError) as caught:
        repo.add_fact(old_id, drawing["evidence"])
    assert caught.value.code == ErrorCode.VERSION_READONLY
    assert repo.create_task(newer["id"])["version_id"] == newer["id"]
    # 历史中的其他项目不因当前项目改版而整体变成只读（Q-11）。
    other = repo.create_project("另一历史项目")
    other_version = repo.create_version(other["id"], "A")
    assert repo.create_task(other_version["id"])["status"] == "待开始"


def test_new_version_cannot_borrow_old_unit(repo, drawing):
    version = repo.create_version(drawing["project"]["id"], "修订")
    with pytest.raises(StoreError) as caught:
        repo.add_fact(version["id"], drawing["evidence"])
    assert caught.value.code == ErrorCode.INVALID_DATA
    with pytest.raises(StoreError):
        repo.create_unit(version["id"], drawing["original"]["id"], kind="pdf_page", ordinal=2, name="错误引用")


def test_revision_link_preserves_previous_problem(repo, drawing):
    old = issue(repo, drawing)
    newer = repo.create_version(drawing["project"]["id"], "修订")
    file = repo.import_original(newer["id"], drawing["source"])["record"]
    unit = repo.create_unit(newer["id"], file["id"], kind="pdf_page", ordinal=1, name="修订图")
    evidence = Evidence(unit["id"], Source.USER, "2400", BBox(0, 0, 1, 1))
    current = repo.create_issue(newer["id"], "仍存在", evidence, evidence, rule_result={},
                                comparison_tag="仍存在", previous_issue_id=old["id"])
    assert current["comparison_tag"] == "仍存在" and current["previous_issue_id"] == old["id"]
    assert repo.get("issues", old["id"]) == old
    with pytest.raises(StoreError) as caught:
        repo.create_issue(newer["id"], "同版错误", evidence, evidence, rule_result={}, previous_issue_id=current["id"])
    assert caught.value.code == ErrorCode.INVALID_DATA


@pytest.mark.parametrize("values", [(-0.1,0,1,1), (0,0,1.1,1), (0.8,0,0.2,1),
                                   (0,0.9,1,0.1), (float("nan"),0,1,1), (0,0,float("inf"),1)])
def test_bbox_rejects_invalid_coordinates(values):
    with pytest.raises(StoreError) as caught:
        BBox(*values)
    assert caught.value.code == ErrorCode.INVALID_DATA


@pytest.mark.parametrize("table,prefix", [("facts", ""), ("pending_confirmations", ""),
                                          ("issues", "left_"), ("issues", "right_")])
def test_sql_constraints_protect_provenance_and_box(repo, drawing, table, prefix):
    vid = drawing["version"]["id"]
    fact = repo.add_fact(vid, drawing["evidence"])
    pending = repo.create_pending(vid, drawing["evidence"], "看不清")
    problem = issue(repo, drawing)
    identifier = {"facts": fact, "pending_confirmations": pending, "issues": problem}[table]["id"]
    for column, value in (("source", None), ("source", "无来源"), ("unit_id", None),
                          ("unit_id", "不存在"), ("x0", -1), ("x1", 1.1), ("y1", 0.1)):
        with pytest.raises(sqlite3.IntegrityError):
            repo._connection.execute(f"UPDATE {table} SET {prefix}{column}=? WHERE id=?", (value, identifier))


def test_settings_snapshots_calls_association_and_export(repo, drawing):
    vid = drawing["version"]["id"]
    settings = repo.get_settings()
    assert settings["dimension_tolerance_mm"] == 1 and settings["elevation_tolerance_m"] == 0.005
    assert settings["monthly_limit_yuan"] is None
    model = {"provider": "本地模型", "model": "mock-only", "base_url": "http://127.0.0.1:9999", "credential_ref": "os-vault-id"}
    repo.save_settings({"primary_model": model, "review_model": None, "monthly_limit_yuan": 10})
    task = repo.create_task(vid)
    repo.save_settings({"dimension_tolerance_mm": 2})
    assert repo.get("review_tasks", task["id"])["tolerance_snapshot"]["dimension_tolerance_mm"] == 1
    with repo.review_session(task["id"]):
        task = repo.update_task(task["id"], status="暂停", checkpoints={"识别": {"cursor": 1}},
                               incomplete=[{"unit_id": drawing["unit"]["id"], "reason": "模拟费用暂停"}], estimated_cost_micros=125000)
    call = repo.record_model_call(task["id"], drawing["unit"]["id"], role="primary", provider="模拟服务商", model="mock-only",
                                  status="成功", input_tokens=10, output_tokens=3, estimated_cost_micros=125000,
                                  image_sha256="a"*64, prompt_version="v1")
    assert call["estimated_cost_micros"] == task["estimated_cost_micros"]
    assert task["checkpoints"]["识别"]["cursor"] == 1 and len(task["incomplete"]) == 1
    association = repo.add_association(vid, drawing["unit"]["id"], drawing["unit"]["id"], "详图索引", {"索引": "A"})
    assert association["details"]["索引"] == "A"
    relative = f"projects/{drawing['project']['id']}/exports/中文 问题清单.xlsx"
    (repo.root / relative).write_bytes(b"TEST-EXPORT")
    exported = repo.record_export(task["id"], format="xlsx", relative_path=relative)
    assert len(exported["sha256"]) == 64
    with Repository(repo.root) as reopened:
        assert reopened.get_settings()["primary_model"] == model
        assert reopened.get("review_tasks", task["id"]) == task
        assert reopened.get("exports", exported["id"]) == exported


@pytest.mark.parametrize("changes", [{"api_key": "禁止明文"}, {"primary_model": {"api_key": "禁止明文"}},
                                     {"dimension_tolerance_mm": -1}, {"monthly_limit_yuan": 0},
                                     {"elevation_tolerance_m": float("nan")}, {"cost_warning_ratio": 1.1}])
def test_invalid_settings_are_atomic(repo, changes):
    before = repo.get_settings()
    with pytest.raises(StoreError) as caught:
        repo.save_settings(changes)
    assert caught.value.code == ErrorCode.INVALID_DATA
    assert repo.get_settings() == before


def test_error_for_missing_or_unknown_record(repo):
    with pytest.raises(StoreError) as caught:
        repo.get("projects", "不存在")
    assert caught.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(StoreError) as caught:
        repo.get("projects; DROP TABLE projects", "任意")
    assert caught.value.code == ErrorCode.INVALID_DATA
