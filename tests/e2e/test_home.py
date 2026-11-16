"""A1：空状态与本机隔离。"""
from urllib.parse import urlparse
from playwright.sync_api import expect


def test_idle_copy_and_real_settings(page, client, dc_server, screenshot):
    assert page.title() == "图纸审查"
    expect(page.locator("body")).to_have_attribute("data-state", "idle")
    text = page.locator("body").inner_text()
    assert "把图纸拖进来，或点导入" in text
    assert "支持 PDF、DWG · 双击安装即可用，数据只存本机" in text
    assert "还没配置模型 · 去设置" in text
    expect(page.get_by_test_id("btn-review")).to_be_disabled()
    expect(page.get_by_test_id("btn-export")).to_be_disabled()
    assert client.get("/api/settings").json()["model_status"] == "none"
    assert dc_server.token not in text
    assert "容差" not in page.locator("body").text_content()
    screenshot(page, "01-idle.png")


def test_no_external_requests_or_console_errors(page):
    external, errors = [], []
    page.on("request", lambda request: external.append(request.url) if urlparse(request.url).hostname not in (None, "127.0.0.1") else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.reload(wait_until="networkidle")
    assert external == []
    assert errors == []
    assert page.evaluate("localStorage.length") == 0
