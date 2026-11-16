from __future__ import annotations

import json
import os
from unittest.mock import patch

import httpx
import pytest

from backend.models.errors import (
    CODE_AUTH_INVALID,
    CODE_BAD_RESPONSE,
    CODE_CERTIFICATE_ERROR,
    CODE_PROXY_ERROR,
    CODE_RATE_LIMITED,
    CODE_TIMEOUT,
    ModelError,
)
from backend.models.mock import MockModelClient
from backend.models.openai_compat import OpenAICompatClient, SecretStr


@pytest.mark.parametrize(
    "branch,code",
    [
        (MockModelClient.BRANCH_SUCCESS, None),
        (MockModelClient.BRANCH_TIMEOUT, CODE_TIMEOUT),
        (MockModelClient.BRANCH_RATE_LIMIT, CODE_RATE_LIMITED),
        (MockModelClient.BRANCH_BAD_FORMAT, CODE_BAD_RESPONSE),
        (MockModelClient.BRANCH_LOW_CONFIDENCE, None),
        (MockModelClient.BRANCH_CERT, CODE_CERTIFICATE_ERROR),
        (MockModelClient.BRANCH_PROXY, CODE_PROXY_ERROR),
    ],
)
def test_mock_branches(branch, code):
    client = MockModelClient(branch=branch)
    if code:
        with pytest.raises(ModelError) as ei:
            client.chat_completions(messages=[{"role": "user", "content": "x"}])
        assert ei.value.code == code
    else:
        result = client.chat_completions(messages=[{"role": "user", "content": "x"}])
        if branch == MockModelClient.BRANCH_LOW_CONFIDENCE:
            data = json.loads(result.content)
            assert data["confidence"] < 0.5
            assert result.extras.get("confidence", 1) < 0.5
        else:
            assert result.input_tokens is not None


def test_mock_trigger_by_input_tag():
    client = MockModelClient(branch=MockModelClient.BRANCH_SUCCESS)
    with pytest.raises(ModelError) as ei:
        client.chat_completions(messages=[{"role": "user", "content": "[mock:timeout]"}])
    assert ei.value.code == CODE_TIMEOUT


def test_secret_str_redaction():
    s = SecretStr("super-secret-value-should-not-leak")
    assert "super-secret" not in repr(s)
    assert "super-secret" not in str(s)
    assert str(s) == "已设置"
    assert s.get().startswith("super-secret")


def test_adapter_maps_401_exactly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client = OpenAICompatClient(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key=SecretStr("fake-key-for-test-only"),
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    with pytest.raises(ModelError) as ei:
        client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == CODE_AUTH_INVALID


def test_adapter_maps_429_with_retries_then_ok_path_exhausted():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"message": "rate"}})

    client = OpenAICompatClient(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key=SecretStr("fake-key-for-test-only"),
        transport=httpx.MockTransport(handler),
        max_retries=2,
        retry_backoff_sec=0,
    )
    with pytest.raises(ModelError) as ei:
        client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == CODE_RATE_LIMITED
    assert calls["n"] == 3


def test_adapter_other_http_keeps_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    client = OpenAICompatClient(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key=SecretStr("fake-key-for-test-only"),
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    with pytest.raises(ModelError) as ei:
        client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == "OTHER_HTTP_503"
    assert ei.value.http_status == 503


def test_adapter_other_http_400():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    client = OpenAICompatClient(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key=SecretStr("fake-key-for-test-only"),
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    with pytest.raises(ModelError) as ei:
        client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == "OTHER_HTTP_400"


def test_adapter_success_and_missing_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" in request.headers
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                # 无 usage
            },
        )

    client = OpenAICompatClient(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key=SecretStr("fake-key-for-test-only"),
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    result = client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    assert result.content == "ok"
    assert result.input_tokens is None


def test_adapter_bad_json_format():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = OpenAICompatClient(
        provider="siliconflow",
        base_url="https://api.example.test/v1",
        api_key=SecretStr("fake-key-for-test-only"),
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    with pytest.raises(ModelError) as ei:
        client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == CODE_BAD_RESPONSE


def test_socks_missing_dependency_maps_proxy_error(monkeypatch):
    """M1：有 SOCKS 代理但缺 socksio → PROXY_ERROR，不崩溃。"""
    client = OpenAICompatClient(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key=SecretStr("fake-key-for-test-only"),
        max_retries=0,
        trust_env=True,
    )

    real_client = httpx.Client

    def boom(*args, **kwargs):
        raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")

    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setattr(httpx, "Client", boom)
    with pytest.raises(ModelError) as ei:
        client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == CODE_PROXY_ERROR
    monkeypatch.setattr(httpx, "Client", real_client)


def test_network_guard_blocks_real_connect():
    client = OpenAICompatClient(
        provider="siliconflow",
        base_url="https://example.invalid/v1",
        api_key=SecretStr("fake-key-for-test-only"),
        max_retries=0,
        trust_env=False,
    )
    with pytest.raises(Exception) as ei:
        client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    assert "网络" in str(ei.value) or isinstance(ei.value, ModelError)
