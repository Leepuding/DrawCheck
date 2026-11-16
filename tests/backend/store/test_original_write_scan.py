"""静态约束和真实操作中的文件打开模式双重检查。"""
from pathlib import Path

from backend.store import Repository
from scan_original_writes import scan


def test_ast_and_text_scan_has_no_rewrite_path():
    result = scan()
    assert result["越界项"] == []
    assert sum(row["模式"] == "xb" for row in result["文件打开"]) == 1


def test_runtime_original_opens_are_readonly_after_import(tmp_path, monkeypatch):
    source = tmp_path / "原始 图（中文）.pdf"
    source.write_bytes(b"TEST-ORIGINAL")
    calls = []
    original_open = Path.open
    def tracked_open(path, mode="r", *args, **kwargs):
        if "originals" in path.parts or path == source:
            calls.append((path, mode))
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", tracked_open)
    with Repository(tmp_path / "data") as repo:
        project = repo.create_project("项目")
        version = repo.create_version(project["id"], "A")
        record = repo.import_original(version["id"], source)["record"]
        assert sum(mode == "xb" for _, mode in calls) == 1
        assert all(mode == "rb" for path, mode in calls if path == source)
        calls.clear()
        repo.import_original(version["id"], source)
        task = repo.create_task(version["id"])
        with repo.review_session(task["id"]):
            repo.original_path(record["id"])
            repo.update_task(task["id"], status="暂停", checkpoints={}, incomplete=["模拟未完成"], estimated_cost_micros=0)
        assert calls and all(mode == "rb" for _, mode in calls)
