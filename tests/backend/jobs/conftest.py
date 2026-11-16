"""jobs 测试：默认断网；live 无 DRAWCHECK_LIVE_MODEL=1 则 skip。"""
from __future__ import annotations

import os
import socket
from pathlib import Path

import httpx
import pytest

from backend.settings.credentials import CredentialService, MemoryCredentialBackend
from backend.settings.service import SettingsService
from backend.store import Repository


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
def repo(data_dir):
    with Repository(data_dir) as store:
        yield store


@pytest.fixture
def settings_svc(repo):
    creds = CredentialService(MemoryCredentialBackend(), allow_env=False)
    return SettingsService(repo, credentials=creds)


@pytest.fixture
def drawing(repo, tmp_path):
    source = tmp_path / "demo.pdf"
    source.write_bytes(b"%PDF-TEST\n")
    project = repo.create_project("测试项目")
    version = repo.create_version(project["id"], "第一版")
    original = repo.import_original(version["id"], source)["record"]
    unit = repo.create_unit(
        version["id"],
        original["id"],
        kind="pdf_page",
        ordinal=1,
        name="演示图",
        drawing_number="A-01",
        metadata={},
    )
    return {
        "project": project,
        "version": version,
        "original": original,
        "unit": unit,
        "source": source,
    }


def configure_single_model(settings_svc: SettingsService, *, monthly_limit_yuan: float = 100):
    return settings_svc.save_settings(
        {
            "monthly_limit_yuan": monthly_limit_yuan,
            "dimension_tolerance_mm": 1,
            "elevation_tolerance_m": 0.005,
            "primary_model": {
                "provider": "siliconflow",
                "model": "zai-org/GLM-4.5V",
                "base_url": "https://api.siliconflow.cn/v1",
                "credential_ref": "",
            },
            "review_model": None,
        },
        primary_api_key="mock-key-primary",
    )


def configure_dual_model(settings_svc: SettingsService, *, monthly_limit_yuan: float = 100, same: bool = False):
    review_model = {
        "provider": "siliconflow",
        "model": "zai-org/GLM-4.5V" if same else "Qwen/Qwen2.5-VL-72B-Instruct",
        "base_url": "https://api.siliconflow.cn/v1",
        "credential_ref": "",
    }
    return settings_svc.save_settings(
        {
            "monthly_limit_yuan": monthly_limit_yuan,
            "dimension_tolerance_mm": 1,
            "elevation_tolerance_m": 0.005,
            "primary_model": {
                "provider": "siliconflow",
                "model": "zai-org/GLM-4.5V",
                "base_url": "https://api.siliconflow.cn/v1",
                "credential_ref": "",
            },
            "review_model": review_model,
        },
        primary_api_key="mock-key-primary",
        review_api_key="mock-key-review",
    )
