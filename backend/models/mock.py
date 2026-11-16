"""Mock 模型：成功 / 超时 / 限流 / 格式不对 / 低把握度。"""
from __future__ import annotations

import json
from typing import Any

from .errors import (
    CODE_BAD_RESPONSE,
    CODE_CERTIFICATE_ERROR,
    CODE_LOW_CONFIDENCE,
    CODE_PROXY_ERROR,
    CODE_RATE_LIMITED,
    CODE_TIMEOUT,
    ModelError,
)
from .openai_compat import CallResult, SecretStr


class MockModelClient:
    """按配置或输入触发五种分支；不发起真实网络。"""

    BRANCH_SUCCESS = "success"
    BRANCH_TIMEOUT = "timeout"
    BRANCH_RATE_LIMIT = "rate_limit"
    BRANCH_BAD_FORMAT = "bad_format"
    BRANCH_LOW_CONFIDENCE = "low_confidence"
    BRANCH_CERT = "certificate_error"
    BRANCH_PROXY = "proxy_error"

    def __init__(self, *, branch: str = BRANCH_SUCCESS, provider: str = "mock", model: str = "mock-vl"):
        self.branch = branch
        self.provider = provider
        self.model = model
        self.api_key = SecretStr("mock-key")
        self.calls = 0
        self.max_retries = 2
        self.retry_backoff_sec = 0.0

    def chat_completions(self, *, model: str | None = None, messages: list[dict[str, Any]] | None = None,
                         **_kwargs) -> CallResult:
        self.calls += 1
        branch = self._resolve_branch(messages)
        model = model or self.model
        if branch == self.BRANCH_TIMEOUT:
            raise ModelError(CODE_TIMEOUT, "模拟超时", retryable=True)
        if branch == self.BRANCH_RATE_LIMIT:
            raise ModelError(CODE_RATE_LIMITED, "模拟限流", retryable=True)
        if branch == self.BRANCH_BAD_FORMAT:
            raise ModelError(CODE_BAD_RESPONSE, "返回格式不对")
        if branch == self.BRANCH_CERT:
            raise ModelError(CODE_CERTIFICATE_ERROR, "证书错误")
        if branch == self.BRANCH_PROXY:
            raise ModelError(CODE_PROXY_ERROR, "代理错误")
        if branch == self.BRANCH_LOW_CONFIDENCE:
            payload = {"text": "看不清", "confidence": 0.2, "flag": "low_confidence"}
            return CallResult(
                content=json.dumps(payload, ensure_ascii=False),
                input_tokens=10,
                output_tokens=5,
                raw_status=200,
                model=model,
                provider=self.provider,
                latency_ms=1,
                extras={"confidence": 0.2},
            )
        # success
        payload = {"text": "2400", "confidence": 0.95}
        return CallResult(
            content=json.dumps(payload, ensure_ascii=False),
            input_tokens=12,
            output_tokens=8,
            raw_status=200,
            model=model,
            provider=self.provider,
            latency_ms=1,
            extras={"confidence": 0.95},
        )

    def test_connection(self, *, model: str, supports_vision: bool = True,
                        image_data_url: str | None = None, prompt: str = "ok") -> CallResult:
        messages = [{"role": "user", "content": prompt}]
        if image_data_url:
            messages[0]["content"] = [
                {"type": "image_url", "image_url": {"url": image_data_url}},
                {"type": "text", "text": prompt},
            ]
        return self.chat_completions(model=model, messages=messages)

    def _resolve_branch(self, messages: list[dict[str, Any]] | None) -> str:
        if messages:
            blob = json.dumps(messages, ensure_ascii=False)
            for name in (
                self.BRANCH_TIMEOUT,
                self.BRANCH_RATE_LIMIT,
                self.BRANCH_BAD_FORMAT,
                self.BRANCH_LOW_CONFIDENCE,
                self.BRANCH_CERT,
                self.BRANCH_PROXY,
            ):
                if f"[mock:{name}]" in blob:
                    return name
        return self.branch


def assert_not_dual_path_same_model(primary_model: str, review_model: str) -> None:
    """防御：禁止同模型双跑冒充复核的代码路径。"""
    if primary_model and review_model and primary_model == review_model:
        raise ModelError(CODE_BAD_RESPONSE, "禁止同一模型跑两遍冒充复核")
