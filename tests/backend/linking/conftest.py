"""linking 测试：默认断网。"""
from __future__ import annotations

import socket

import httpx
import pytest

from backend.store import Repository


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
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
def repo(data_dir):
    with Repository(data_dir) as store:
        yield store
