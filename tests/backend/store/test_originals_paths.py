"""原图保护、重复导入、数据目录权限、路径逃逸实测。"""
import hashlib
import os
from pathlib import Path

import pytest

from backend.store import ErrorCode, Repository, StoreError
from backend.store.paths import data_root, safe_path


def test_original_readonly_hash_before_after_review(repo, drawing):
    source = drawing["source"]
    source_mode = source.stat().st_mode
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    original = drawing["original"]
    path = repo.original_path(original["id"])
    assert not Path(original["relative_path"]).is_absolute()
    assert path != source and not path.stat().st_mode & 0o222
    assert original["sha256"] == digest == hashlib.sha256(path.read_bytes()).hexdigest()
    if os.name == "nt" or os.geteuid() != 0:
        with pytest.raises(PermissionError):
            path.open("r+b")
    before = repo.verify_originals(drawing["version"]["id"])
    task = repo.create_task(drawing["version"]["id"])
    with repo.review_session(task["id"]):
        repo.update_task(task["id"], status="已完成", checkpoints={"模拟检查": "完成"}, incomplete=[], estimated_cost_micros=0)
    assert repo.verify_originals(drawing["version"]["id"]) == before
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert source.stat().st_mode == source_mode


def test_duplicate_import_skips_and_cross_version_reuses(repo, drawing, tmp_path):
    copy = tmp_path / "另一个 名称.dwg"
    copy.write_bytes(drawing["source"].read_bytes())
    duplicate = repo.import_original(drawing["version"]["id"], copy)
    assert duplicate["reused"] is True and duplicate["record"] == drawing["original"]
    newer = repo.create_version(drawing["project"]["id"], "修订")
    imported = repo.import_original(newer["id"], copy)
    assert imported["record"]["id"] != drawing["original"]["id"]
    assert imported["record"]["relative_path"] == drawing["original"]["relative_path"]
    assert len(list((repo.root / f"projects/{drawing['project']['id']}/originals").iterdir())) == 1


def test_corruption_is_detected_never_repaired(repo, drawing):
    path = repo.original_path(drawing["original"]["id"])
    path.chmod(0o644)
    path.write_bytes("模拟外部篡改".encode())
    path.chmod(0o444)
    for action in (lambda: repo.original_path(drawing["original"]["id"]),
                   lambda: repo.import_original(drawing["version"]["id"], drawing["source"])):
        with pytest.raises(StoreError) as caught:
            action()
        assert caught.value.code == ErrorCode.ORIGINAL_CHANGED
    assert path.read_bytes() == "模拟外部篡改".encode()


def test_review_checks_integrity_even_when_processing_fails(repo, drawing):
    task = repo.create_task(drawing["version"]["id"])
    path = repo.original_path(drawing["original"]["id"])
    with pytest.raises(StoreError) as caught:
        with repo.review_session(task["id"]):
            path.chmod(0o644)
            raise RuntimeError("模拟处理失败")
    assert caught.value.code == ErrorCode.ORIGINAL_CHANGED
    path.chmod(0o444)


def test_original_record_cannot_be_repointed(repo, drawing):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="ORIGINAL_IMMUTABLE"):
        repo._connection.execute("UPDATE original_files SET relative_path='exports/a' WHERE id=?", (drawing["original"]["id"],))


def test_replace_cannot_bypass_original_immutability(repo, drawing):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="ORIGINAL_IMMUTABLE"):
        repo._connection.execute("INSERT OR REPLACE INTO original_files SELECT * FROM original_files WHERE id=?",
                                 (drawing["original"]["id"],))
    assert repo.get("original_files", drawing["original"]["id"]) == drawing["original"]


def test_source_change_during_import_is_not_registered(repo, tmp_path, monkeypatch):
    import backend.store.repository as module
    source = tmp_path / "导入时变化.pdf"
    source.write_bytes(b"before")
    project = repo.create_project("项目")
    version = repo.create_version(project["id"], "A")
    actual_hash = module.sha256_file
    def changing_hash(path):
        digest = actual_hash(path)
        source.write_bytes(b"after")
        return digest
    monkeypatch.setattr(module, "sha256_file", changing_hash)
    with pytest.raises(StoreError) as caught:
        repo.import_original(version["id"], source)
    assert caught.value.code == ErrorCode.ORIGINAL_CHANGED
    assert repo.list_records("original_files", version_id=version["id"]) == []


@pytest.mark.skipif(os.name == "nt", reason="chmod 目录权限是 POSIX 实测；Windows 需真实 ACL 环境")
def test_directory_permission_denial_is_explicit(tmp_path):
    blocked = tmp_path / "无写权限"
    blocked.mkdir()
    blocked.chmod(0o555)
    try:
        with pytest.raises(StoreError) as caught:
            Repository(blocked)
        assert caught.value.code == ErrorCode.DATA_DIR_NOT_WRITABLE
    finally:
        blocked.chmod(0o755)


@pytest.mark.parametrize("relative", ["../outside", "/tmp/outside", "C:\\Windows\\x", "projects/../../x"])
def test_path_escape_is_rejected(tmp_path, relative):
    with pytest.raises(StoreError) as caught:
        safe_path(tmp_path, relative)
    assert caught.value.code == ErrorCode.INVALID_PATH


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "projects").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StoreError) as caught:
        safe_path(root, "projects/abc/originals")
    assert caught.value.code == ErrorCode.INVALID_PATH


def test_internal_symlink_cannot_redirect_cache_to_originals(tmp_path):
    (tmp_path / "originals").mkdir()
    (tmp_path / "cache").symlink_to(tmp_path / "originals", target_is_directory=True)
    with pytest.raises(StoreError) as caught:
        safe_path(tmp_path, "cache/new.pdf")
    assert caught.value.code == ErrorCode.INVALID_PATH


def test_permission_error_from_probe_is_not_swallowed(tmp_path, monkeypatch):
    import backend.store.paths as module
    def denied(*_args, **_kwargs):
        raise PermissionError(13, "模拟 Windows ACL 或 POSIX 权限拒绝")
    monkeypatch.setattr(module.tempfile, "TemporaryFile", denied)
    with pytest.raises(StoreError) as caught:
        Repository(tmp_path)
    assert caught.value.code == ErrorCode.DATA_DIR_NOT_WRITABLE


def test_data_directory_defaults_and_override(monkeypatch, tmp_path):
    import backend.store.paths as module
    monkeypatch.setenv("DRAWCHECK_DATA_DIR", str(tmp_path / "开发 数据"))
    assert data_root() == tmp_path / "开发 数据"
    monkeypatch.delenv("DRAWCHECK_DATA_DIR")
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert data_root() == tmp_path / "Local/DrawCheck"
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert data_root() == tmp_path / "xdg/DrawCheck"
    monkeypatch.setattr(module.sys, "platform", "darwin")
    assert data_root() == Path.home() / "Library/Application Support/DrawCheck"


def test_export_path_cannot_point_at_original(repo, drawing):
    task = repo.create_task(drawing["version"]["id"])
    with pytest.raises(StoreError) as caught:
        repo.record_export(task["id"], format="pdf", relative_path=drawing["original"]["relative_path"])
    assert caught.value.code == ErrorCode.INVALID_PATH
