from __future__ import annotations

import socket

import httpx
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: 真实模型调用（需 DRAWCHECK_LIVE_MODEL=1）")


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    if request.node.get_closest_marker("live"):
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
