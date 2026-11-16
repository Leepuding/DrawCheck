"""DC-15：问题列表与复核 PATCH 持久化。"""
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
from backend.store.models import BBox, Evidence, Source


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


def test_seed_list_patch_persist(api_env, tmp_path):
    client, data_dir = api_env
    version_id, _ = _import_version(client, tmp_path)
    seeded = client.post("/api/issues/seed-mock", json={"version_id": version_id})
    assert seeded.status_code == 200, seeded.text
    body = seeded.json()
    assert body["issue_count"] >= 1
    assert body["items"][0]["review_status"] == "疑似"
    issue_id = body["items"][0]["id"]
    box = body["items"][0]["main_bbox"]
    assert 0 <= box["x0"] <= 1 and box["x1"] >= box["x0"]

    patched = client.patch(f"/api/issues/{issue_id}", json={"review_status": "确认", "review_note": ""})
    assert patched.status_code == 200, patched.text
    assert patched.json()["review_status"] == "已确认"

    listed = client.get("/api/issues", params={"version_id": version_id}).json()
    row = next(item for item in listed["items"] if item["id"] == issue_id)
    assert row["review_status"] == "已确认"

    # 同一数据目录新进程读库仍在
    with Repository(data_dir) as repo:
        assert repo.get("issues", issue_id)["review_status"] == "已确认"

    # 再点当前态 → 疑似
    toggled = client.patch(f"/api/issues/{issue_id}", json={"review_status": "已确认", "toggle": True})
    assert toggled.json()["review_status"] == "疑似"

    excluded = client.patch(f"/api/issues/{issue_id}", json={"review_status": "排除"})
    assert excluded.json()["review_status"] == "排除"
    ordered = client.get("/api/issues", params={"version_id": version_id}).json()["items"]
    assert ordered[-1]["id"] == issue_id

    bad = client.patch(f"/api/issues/{issue_id}", json={"review_status": "通过"})
    assert bad.status_code >= 400


def test_unit_image_and_404(api_env, tmp_path):
    client, _ = api_env
    version_id, result = _import_version(client, tmp_path)
    unit_id = result["drawings"][0]["unit_id"]
    ok = client.get(f"/api/units/{unit_id}/image")
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/")
    missing = client.get("/api/units/does-not-exist/image")
    assert missing.status_code == 404
