from __future__ import annotations

import re
import secrets
import threading
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright

from backend.server import start_server

FORBIDDEN_TEXT = re.compile(r"(127\.0\.0\.1|localhost)", re.I)
# 令牌与端口不在 DOM 文本中：端口用纯数字误伤大，改为检查常见泄漏模式
TOKEN_LEAK = re.compile(r"(__launch\?t=|Bearer\s+\S+|drawcheck_token=)", re.I)


@pytest.fixture(scope="module")
def live_server():
    token = secrets.token_urlsafe(18)
    server = start_server(token)
    try:
        yield server
    finally:
        server.stop(join_timeout=3.0)


def test_shell_empty_state_and_isolation(live_server):
    external = []
    console_errors = []
    launch = f"{live_server.base_url}/__launch?t={live_server.token}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        def on_request(request):
            url = request.url
            host = urlparse(url).hostname or ""
            if host and host not in ("127.0.0.1", "localhost"):
                external.append(url)

        def on_console(msg):
            if msg.type == "error":
                console_errors.append(msg.text)

        page.on("request", on_request)
        page.on("console", on_console)

        page.goto(launch, wait_until="networkidle")
        assert page.title() == "图纸审查"

        # 空状态与三按钮
        assert page.locator('[data-testid="btn-import"]').is_visible()
        assert page.locator('[data-testid="btn-review"]').is_disabled()
        assert page.locator('[data-testid="btn-export"]').is_disabled()
        body_text = page.locator("body").inner_text()
        assert "把图纸拖进来，或点导入" in body_text
        assert "支持 PDF、DWG · 双击安装即可用，数据只存本机" in body_text
        assert "原型演示" not in body_text
        assert "重置原型" not in body_text

        # DOM 文本不得出现本机地址；亦不得出现换票串/Bearer
        assert not FORBIDDEN_TEXT.search(body_text)
        assert not TOKEN_LEAK.search(body_text)
        # 端口数字：确认页面正文不含 ":{port}" 或 "端口"
        assert f":{live_server.port}" not in body_text
        assert live_server.token not in body_text
        assert "端口" not in body_text

        # 外部请求为 0
        assert external == []
        # 控制台错误为 0
        assert console_errors == []

        # 健康检查已用 cookie 成功（由 app.js ping）
        health = page.evaluate(
            """async () => {
              const r = await fetch('/api/health', { credentials: 'same-origin' });
              return { status: r.status, body: await r.json() };
            }"""
        )
        assert health["status"] == 200
        assert health["body"]["status"] == "ok"

        context.close()
        browser.close()
