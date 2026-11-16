"""DC-16：待确认 API、用户确认事实、重算钩子与模型调用不增加。"""
from __future__ import annotations

import secrets
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from backend.models.mock import MockModelClient
from backend.parsers.pdf import PDFParser
from backend.server import start_server
from backend.settings.credentials import CredentialService, MemoryCredentialBackend
from backend.settings.service import SettingsService
from backend.store import Repository


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAWCHECK_ENABLE_TEST_ROUTES", "1")
    data = tmp_path / "data"
    monkeypatch.setenv("DRAWCHECK_DATA_DIR", str(data))
    credentials = MemoryCredentialBackend()
    monkeypatch.setattr(CredentialService, "_detect_backend", lambda self: credentials)
    monkeypatch.setattr(SettingsService, "_make_client", lambda self, slot, secret: MockModelClient())

    def render(command, timeout):
        if command[0] == "pdftoppm":
            Image.new("RGB", (200, 120), "white").save(Path(command[-1]).with_suffix(".png"))

    monkeypatch.setattr(PDFParser, "_run", staticmethod(render))
    server = start_server(secrets.token_urlsafe(12))
    try:
        with httpx.Client(base_url=server.base_url, trust_env=False,
                          headers={"Authorization": f"Bearer {server.token}"}, timeout=20) as client:
            yield client, data
    finally:
        server.stop(join_timeout=5)


def _pdf(path: Path):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=120)
    writer.add_blank_page(width=200, height=120)
    writer.write(path)


def _import_version(client, tmp_path):
    pdf = tmp_path / "两页.pdf"
    _pdf(pdf)
    with pdf.open("rb") as handle:
        submitted = client.post("/api/import", files=[("files", ("两页.pdf", handle, "application/pdf"))])
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    job = None
    for _ in range(80):
        job = client.get(f"/api/import/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert job and job["status"] == "completed", job
    return job["result"]["version_id"], job["result"]


def test_seed_list_confirm_unsure_and_recount(api_env, tmp_path):
    client, data_dir = api_env
    version_id, _ = _import_version(client, tmp_path)
    seeded = client.post("/api/pending/seed-mock", json={"version_id": version_id})
    assert seeded.status_code == 200, seeded.text
    body = seeded.json()
    assert body["pending_count"] == 2
    assert len(body["items"]) == 2
    assert all(item["status"] == "待确认" for item in body["items"])
    assert body["incomplete_pending_count"] == 2
    first, second = body["items"][0], body["items"][1]
    box = first["bbox"]
    assert 0 <= box["x0"] <= 1 and box["x1"] >= box["x0"]

    # 保存：已确认 + 用户确认事实 + 模型调用不增加 + 实际本地重算
    saved = client.patch(f"/api/pending/{first['id']}", json={"status": "已确认", "confirmed_text": "2400"})
    assert saved.status_code == 200, saved.text
    payload = saved.json()
    assert payload["item"]["status"] == "已确认"
    assert payload["item"]["confirmed_text"] == "2400"
    assert payload["item"]["participates_in_comparison"] is True
    assert payload["pending_count"] == 1
    assert payload["fact_source"] == "用户确认"
    assert payload["model_calls_delta"] == 0
    assert payload["recalc"]["skipped"] is False
    assert payload["recalc"]["reason"] is None

    listed = client.get("/api/pending", params={"version_id": version_id}).json()
    assert listed["pending_count"] == 1
    issues = client.get("/api/issues", params={"version_id": version_id}).json()
    assert issues["pending_count"] == 1

    with Repository(data_dir) as repo:
        row = repo.get("pending_confirmations", first["id"])
        assert row["status"] == "已确认"
        assert row["confirmed_text"] == "2400"
        facts = repo.list_records("facts", version_id=version_id)
        user_facts = [f for f in facts if f.get("source") == "用户确认"]
        assert user_facts and user_facts[-1]["text"] == "2400"
        assert len(repo.list_records("model_calls", version_id=version_id)) == 0

    # 无法确定
    unsure = client.patch(f"/api/pending/{second['id']}", json={"status": "无法确定"})
    assert unsure.status_code == 200, unsure.text
    assert unsure.json()["item"]["status"] == "无法确定"
    assert unsure.json()["item"]["participates_in_comparison"] is False
    assert unsure.json()["item"]["counts_incomplete"] is True
    assert unsure.json()["pending_count"] == 0
    assert unsure.json()["incomplete_pending_count"] == 1
    assert unsure.json()["model_calls_delta"] == 0

    # W7-Q1=A：已处理可再改（改值），计数仍只计待确认
    reedit = client.patch(
        f"/api/pending/{first['id']}",
        json={"status": "已确认", "confirmed_text": "2500"},
    )
    assert reedit.status_code == 200, reedit.text
    assert reedit.json()["item"]["confirmed_text"] == "2500"
    assert reedit.json()["pending_count"] == 0
    assert reedit.json()["model_calls_delta"] == 0
    assert reedit.json()["recalc"]["skipped"] is False

    # 再改为无法确定
    to_unsure = client.patch(f"/api/pending/{first['id']}", json={"status": "无法确定"})
    assert to_unsure.json()["item"]["status"] == "无法确定"
    assert to_unsure.json()["incomplete_pending_count"] == 2
    assert to_unsure.json()["pending_count"] == 0

    # 校验
    bad = client.patch(f"/api/pending/{first['id']}", json={"status": "已确认", "confirmed_text": ""})
    assert bad.status_code >= 400
    too_long = client.patch(
        f"/api/pending/{first['id']}",
        json={"status": "已确认", "confirmed_text": "字" * 81},
    )
    assert too_long.status_code >= 400


def test_hook_invoked_when_registry_present(api_env, tmp_path, monkeypatch):
    client, _ = api_env
    version_id, _ = _import_version(client, tmp_path)
    seeded = client.post("/api/pending/seed-mock", json={"version_id": version_id}).json()
    pending_id = seeded["items"][0]["id"]

    calls = {"n": 0}

    class FakeRegistry:
        @staticmethod
        def recalculate_for_pending(repo, version_id, pending):
            calls["n"] += 1
            return {"ok": True, "pending_id": pending["id"]}

    import types
    import sys
    import backend

    fake_rules = types.ModuleType("backend.rules")
    fake_rules.registry = FakeRegistry
    # rebase 后真实 backend.rules 可能已导入；需同时替换 sys.modules 与包属性
    monkeypatch.setitem(sys.modules, "backend.rules", fake_rules)
    monkeypatch.setattr(backend, "rules", fake_rules, raising=False)

    saved = client.patch(
        f"/api/pending/{pending_id}",
        json={"status": "已确认", "confirmed_text": "2410"},
    )
    assert saved.status_code == 200, saved.text
    assert calls["n"] == 1
    assert saved.json()["recalc"]["invoked"] is True
    assert saved.json()["recalc"]["skipped"] is False
    assert saved.json()["model_calls_delta"] == 0
