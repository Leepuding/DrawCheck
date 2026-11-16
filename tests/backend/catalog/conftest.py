"""catalog 测试共享夹具（复用 extract 断网守卫）。"""
from __future__ import annotations

import os
import socket
from pathlib import Path

import httpx
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: 真实模型调用（需 DRAWCHECK_LIVE_MODEL=1）")


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        if os.environ.get("DRAWCHECK_LIVE_MODEL") != "1":
            pytest.skip("未设置 DRAWCHECK_LIVE_MODEL=1，跳过真调用")
        yield
        return

    real_socket = socket.socket

    class GuardedSocket(real_socket):
        def connect(self, address):  # noqa: ANN001
            raise RuntimeError(f"网络已禁用（测试守卫）: {address}")

        def connect_ex(self, address):  # noqa: ANN001
            raise RuntimeError(f"网络已禁用（测试守卫）: {address}")

    monkeypatch.setattr(socket, "socket", GuardedSocket)
    real_transport = httpx.HTTPTransport

    class BlockedTransport(real_transport):
        def handle_request(self, request):  # noqa: ANN001
            raise RuntimeError(f"httpx 网络已禁用: {request.url}")

    monkeypatch.setattr(httpx, "HTTPTransport", BlockedTransport)
    yield


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("DRAWCHECK_DATA_DIR", str(root))
    return root


@pytest.fixture
def repo_root():
    return Path(__file__).resolve().parents[3]


@pytest.fixture
def synthetic_root(repo_root):
    return repo_root / "fixtures" / "synthetic"
