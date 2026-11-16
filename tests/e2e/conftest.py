"""DC-14：独立数据目录、真实 HTTP/浏览器、显式模型与渲染替身。"""
from __future__ import annotations

import secrets
import json
import shutil
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfWriter

from backend.models.mock import MockModelClient
from backend.models.openai_compat import CallResult
from backend.parsers.pdf import PDFParser
from backend.server import start_server
from backend.settings.credentials import CredentialService, MemoryCredentialBackend
from backend.settings.service import SettingsService


@pytest.fixture
def dc_server(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAWCHECK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DRAWCHECK_ENABLE_TEST_ROUTES", "1")
    monkeypatch.setenv("DRAWCHECK_EXPORT_DIR", str(tmp_path / "exports"))
    # 每个测试共享同一内存凭据替身；绝不触碰系统钥匙串或真实模型。
    credentials = MemoryCredentialBackend()
    monkeypatch.setattr(CredentialService, "_detect_backend", lambda self: credentials)
    class StructuredModel(MockModelClient):
        def chat_completions(self, *, model=None, messages=None, **kwargs):
            return CallResult(content=json.dumps({"prompt_version": "dc12-extract-v2", "title_blocks": [], "facts": []}),
                              input_tokens=12, output_tokens=8, raw_status=200, model=model or self.model,
                              provider=self.provider, latency_ms=1, extras={})
    monkeypatch.setattr(SettingsService, "_make_client", lambda self, slot, secret: StructuredModel())
    server = start_server(secrets.token_urlsafe(18))
    try:
        yield server
    finally:
        server.stop(join_timeout=5)
        assert not server.thread.is_alive()


@pytest.fixture
def client(dc_server):
    with httpx.Client(base_url=dc_server.base_url, trust_env=False,
                      headers={"Authorization": f"Bearer {dc_server.token}"}, timeout=15) as client:
        yield client


@pytest.fixture
def page(dc_server):
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.goto(f"{dc_server.base_url}/__launch?t={dc_server.token}", wait_until="networkidle")
        expect(page.get_by_test_id("btn-import")).to_be_enabled()
        yield page
        context.close()
        browser.close()


@pytest.fixture
def pdf_files(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    path = uploads / "自制两页.pdf"
    writer.write(path)
    bad = uploads / "说明.txt"
    bad.write_text("这是自制不支持格式夹具", encoding="utf-8")
    return [str(path), str(bad)]


@pytest.fixture
def rendered_pdf(monkeypatch):
    """缺 Poppler 时只替换外部渲染，仍走真实 pypdf 拆页、导入与 SQLite。"""
    if not (shutil.which("pdftoppm") and shutil.which("pdfinfo")):
        def render(command, timeout):
            if command[0] == "pdftoppm":
                from PIL import Image
                Image.new("RGB", (100, 100), "white").save(Path(command[-1]).with_suffix(".png"))
        monkeypatch.setattr(PDFParser, "_run", staticmethod(render))


@pytest.fixture
def imported(page, pdf_files, rendered_pdf):
    with page.expect_file_chooser() as chooser:
        page.get_by_test_id("btn-import").click()
    chooser.value.set_files(pdf_files)
    expect(page.locator("body")).to_have_attribute("data-state", "imported", timeout=15000)
    return page


@pytest.fixture
def configure(client):
    def apply(dual=False):
        body = {"monthly_limit_yuan": 200, "primary_model": {"provider":"siliconflow"},
                "primary_api_key":"test-dc14-primary", "review_model": None}
        if dual:
            body.update(review_model={"provider":"qwen"}, review_api_key="test-dc14-review")
        response = client.patch("/api/settings", json=body)
        assert response.status_code == 200
        assert response.json()["model_status"] == ("dual" if dual else "single")
        return response.json()
    return apply


@pytest.fixture
def screenshot():
    def save(page, name):
        target = Path("_evidence") / name
        target.parent.mkdir(exist_ok=True)
        page.screenshot(path=str(target), full_page=False)
    return save
