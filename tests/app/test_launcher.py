from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from app.launcher import Application, InstanceLock
from backend.server import start_server


def test_backend_stops_within_3_seconds(tmp_path: Path):
    token = secrets.token_urlsafe(12)
    server = start_server(token)
    port = server.port
    assert httpx.get(
        f"{server.base_url}/api/health",
        headers={"Authorization": f"Bearer {token}"},
        timeout=3.0,
    ).status_code == 200

    t0 = time.time()
    server.stop(join_timeout=3.0)
    elapsed = time.time() - t0
    assert elapsed <= 3.0
    assert not server.thread.is_alive()
    # 端口应不再接受连接
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        # 给系统一点点回收时间
        deadline = time.time() + 1.0
        closed = False
        while time.time() < deadline:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                closed = True
                break
            time.sleep(0.05)
        assert closed


def test_application_start_backend_loopback_and_token():
    app = Application(browser=True)
    server = app.start_backend()
    try:
        assert server.host == "127.0.0.1"
        assert server.port > 0
        assert app.token
        # 无令牌 401
        assert httpx.get(f"{server.base_url}/api/health", timeout=3.0).status_code == 401
        assert httpx.get(
            f"{server.base_url}/api/health",
            headers={"Authorization": f"Bearer {app.token}"},
            timeout=3.0,
        ).status_code == 200
        # 启动 URL 含换票路径，但前端页面本身不渲染令牌
        assert "/__launch?t=" in app.launch_url()
    finally:
        app.shutdown()


def test_second_instance_signals_focus(tmp_path: Path):
    runtime = tmp_path / "run"
    focused = threading.Event()

    first = InstanceLock(runtime_dir=runtime)
    assert first.acquire() is True
    first.start_focus_server(focused.set)

    second = InstanceLock(runtime_dir=runtime)
    assert second.acquire() is False
    assert focused.wait(timeout=2.0) is True

    first.release()
    # 释放后应能再次获取
    third = InstanceLock(runtime_dir=runtime)
    assert third.acquire() is True
    third.release()


def test_shutdown_joins_backend_within_3s():
    app = Application(browser=True)
    app.start_backend()
    assert app.server is not None
    thread = app.server.thread
    t0 = time.time()
    app.shutdown()
    assert time.time() - t0 <= 3.5
    assert not thread.is_alive()
    assert app.server is None
