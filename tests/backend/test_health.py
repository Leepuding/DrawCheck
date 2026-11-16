from __future__ import annotations

import secrets
import socket

import httpx
import pytest

from backend.server import start_server


@pytest.fixture()
def running():
    token = secrets.token_urlsafe(16)
    server = start_server(token, host="127.0.0.1", port=None)
    try:
        yield server
    finally:
        server.stop(join_timeout=3.0)


def test_binds_loopback_only(running):
    assert running.host == "127.0.0.1"
    # 确认监听在 127.0.0.1，且端口为启动时分配的随机端口
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        assert sock.connect_ex(("127.0.0.1", running.port)) == 0


def test_port_is_ephemeral_random():
    a = start_server(secrets.token_urlsafe(8))
    b = start_server(secrets.token_urlsafe(8))
    try:
        assert a.port != b.port
        assert a.port > 0 and b.port > 0
    finally:
        a.stop()
        b.stop()


def test_health_requires_token(running):
    url = f"{running.base_url}/api/health"
    r = httpx.get(url, timeout=3.0)
    assert r.status_code == 401


def test_health_ok_with_bearer(running):
    url = f"{running.base_url}/api/health"
    r = httpx.get(url, headers={"Authorization": f"Bearer {running.token}"}, timeout=3.0)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_ok_with_launch_cookie(running):
    client = httpx.Client(timeout=3.0, follow_redirects=True)
    try:
        r = client.get(f"{running.base_url}/__launch", params={"t": running.token})
        assert r.status_code == 200
        health = client.get(f"{running.base_url}/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
    finally:
        client.close()
