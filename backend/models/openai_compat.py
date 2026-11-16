"""OpenAI 兼容 chat/completions 适配器（httpx，不引入 SDK）。"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .errors import (
    CODE_AUTH_INVALID,
    CODE_BAD_RESPONSE,
    CODE_BALANCE_INSUFFICIENT,
    CODE_CERTIFICATE_ERROR,
    CODE_LOCAL_UNREACHABLE,
    CODE_PROXY_ERROR,
    CODE_RATE_LIMITED,
    CODE_TIMEOUT,
    CODE_VISION_UNSUPPORTED,
    ModelError,
    other_http_code,
    other_net_code,
)
from .presets import normalize_base_url


@dataclass
class CallResult:
    content: str
    input_tokens: int | None
    output_tokens: int | None
    raw_status: int
    model: str
    provider: str
    latency_ms: int
    finish_reason: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    out = {}
    for key, value in (headers or {}).items():
        if key.lower() in {"authorization", "api-key", "x-api-key"}:
            out[key] = "***"
        else:
            out[key] = value
    return out


def _safe_error_body(text: str, limit: int = 400) -> str:
    """截断并去掉可能的密钥片段（Bearer / sk-）。"""
    if not text:
        return ""
    cleaned = text
    for marker in ("Bearer ", "sk-", "api_key", "apiKey"):
        if marker in cleaned:
            cleaned = "[redacted]"
            break
    return cleaned[:limit]


class SecretStr:
    """密钥包装：str/repr 仅显示「已设置」，取值用 .get()。"""

    __slots__ = ("_value",)

    def __init__(self, value: str | None):
        self._value = value or ""

    def get(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __str__(self) -> str:
        return "已设置" if self._value else ""

    def __repr__(self) -> str:
        return "SecretStr(已设置)" if self._value else "SecretStr()"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretStr):
            return self._value == other._value
        return NotImplemented


class OpenAICompatClient:
    """最小 chat/completions 客户端；支持图像 data URL、限流重试、代理与证书错误映射。"""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: SecretStr | str,
        timeout_sec: float = 60.0,
        connect_timeout_sec: float | None = None,
        write_timeout_sec: float | None = None,
        pool_timeout_sec: float | None = None,
        max_retries: int = 2,
        retry_backoff_sec: float = 0.5,
        trust_env: bool = True,
        transport: httpx.BaseTransport | None = None,
    ):
        self.provider = provider
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self.timeout_sec = timeout_sec
        self.connect_timeout_sec = connect_timeout_sec
        self.write_timeout_sec = write_timeout_sec
        self.pool_timeout_sec = pool_timeout_sec
        self.max_retries = max_retries
        self.retry_backoff_sec = retry_backoff_sec
        self.trust_env = trust_env
        self._transport = transport

    def _client(self) -> httpx.Client:
        """构造 httpx.Client；SOCKS 缺 socksio 等依赖时映射为 PROXY_ERROR（M1）。"""
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout_sec,
                connect=self.connect_timeout_sec if self.connect_timeout_sec is not None else self.timeout_sec,
                write=self.write_timeout_sec if self.write_timeout_sec is not None else self.timeout_sec,
                pool=self.pool_timeout_sec if self.pool_timeout_sec is not None else self.timeout_sec),
            "trust_env": self.trust_env,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            return httpx.Client(**kwargs)
        except ImportError as exc:
            msg = str(exc).lower()
            if "socks" in msg or "socksio" in msg:
                raise ModelError(CODE_PROXY_ERROR, "系统代理需要 SOCKS 依赖但未安装") from exc
            raise ModelError(CODE_PROXY_ERROR, "代理依赖缺失") from exc
        except Exception as exc:
            # httpx 在部分环境下构造代理时也可能抛其他异常
            text = str(exc).lower()
            if "socks" in text or "socksio" in text or "proxy" in text:
                raise ModelError(CODE_PROXY_ERROR, "代理错误") from exc
            raise

    def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int | None = 256,
        extra: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> CallResult:
        if not self.base_url:
            raise ModelError(CODE_LOCAL_UNREACHABLE, "缺少服务地址")
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra:
            payload.update(extra)
        if stream:
            payload["stream"] = True

        headers = {
            "Authorization": f"Bearer {self.api_key.get()}",
            "Content-Type": "application/json",
        }
        for attempt in range(self.max_retries + 1):
            try:
                return self._request(model, url, headers, payload, stream)
            except ModelError as exc:
                if not exc.retryable or attempt == self.max_retries or stream:
                    raise
                time.sleep(self.retry_backoff_sec * (attempt + 1))
        raise AssertionError("unreachable retry state")

    def _request(self, model, url, headers, payload, stream):
        started = time.monotonic()

        def elapsed():
            return int((time.monotonic() - started) * 1000)

        diagnostics = {"started_at": datetime.now(timezone.utc).isoformat(), "phase": "response_headers",
                       "stream": stream, "read_timeout_sec": self.timeout_sec, "max_tokens": payload.get("max_tokens")}
        try:
            with self._client() as client, client.stream("POST", url, headers=headers, json=payload) as response:
                diagnostics.update(headers_ms=elapsed(), http_status=response.status_code, phase="response_body")
                trace = response.headers.get("x-siliconcloud-trace-id", "")
                if (re.fullmatch(r"ti_[A-Za-z0-9_-]{1,100}", trace)
                        and trace not in self.api_key.get()):
                    diagnostics["provider_trace_id"] = trace
                if response.status_code >= 400 or not stream:
                    response.read()
                    mapped = self._map_http_error(response)
                    if mapped is not None:
                        raise mapped
                    diagnostics["phase"] = "response_format"
                    try:
                        data = response.json()
                    except (json.JSONDecodeError, UnicodeError) as exc:
                        raise ModelError(CODE_BAD_RESPONSE, "返回非 JSON") from exc
                else:
                    data = self._read_sse(response, diagnostics, elapsed)
                result = self._parse_success(data, model=model, status=response.status_code, latency_ms=elapsed())
        except (ModelError, httpx.HTTPError, OSError, ImportError) as exc:
            error = exc if isinstance(exc, ModelError) else self._transport_error(exc, diagnostics)
            error.diagnostics = {**diagnostics, "elapsed_ms": elapsed(),
                                 "failure_code": error.code, "retryable": error.retryable}
            if error is exc:
                raise
            raise error from exc
        diagnostics.update(phase="complete", elapsed_ms=elapsed(),
                           finish_reason=result.extras.get("finish_reason"),
                           usage_reported=result.input_tokens is not None and result.output_tokens is not None)
        result.extras["request"] = diagnostics
        return result

    def _transport_error(self, exc, diagnostics):
        for kind, phase, reason in (
            (httpx.ConnectTimeout, "connect", "连接模型服务超时"),
            (httpx.WriteTimeout, "upload", "发送模型请求超时"),
            (httpx.PoolTimeout, "pool", "等待模型连接池超时"),
            (httpx.ReadTimeout, diagnostics["phase"], "等待模型响应超时"),
            (httpx.TimeoutException, diagnostics["phase"], "模型请求超时"),
        ):
            if isinstance(exc, kind):
                diagnostics["phase"] = phase
                return ModelError(CODE_TIMEOUT, reason, retryable=True)
        message = str(exc).lower()
        if isinstance(exc, (httpx.ProxyError, ImportError)):
            default = "代理依赖缺失" if isinstance(exc, ImportError) else "代理错误"
            return ModelError(CODE_PROXY_ERROR, "系统代理需要 SOCKS 依赖但未安装" if "socks" in message else default)
        if isinstance(exc, httpx.ConnectError):
            diagnostics["phase"] = "connect"
            if any(word in message for word in ("certificate", "ssl", "cert")):
                return ModelError(CODE_CERTIFICATE_ERROR, "证书错误")
            if self.provider == "local":
                return ModelError(CODE_LOCAL_UNREACHABLE, "本地地址连不上")
            return ModelError(CODE_TIMEOUT, "网络不通")
        if isinstance(exc, OSError) and any(word in message for word in ("socks", "proxy")):
            return ModelError(CODE_PROXY_ERROR, "代理错误")
        label = "HTTP 客户端错误" if isinstance(exc, httpx.HTTPError) else "网络错误"
        return ModelError(other_net_code(exc), f"{label}（{type(exc).__name__}）",
                          retryable=diagnostics["stream"] and isinstance(exc, (httpx.ReadError, httpx.RemoteProtocolError)))

    @staticmethod
    def _read_sse(response, diagnostics, elapsed):
        # Only the chosen answer becomes evidence. Reasoning, error payloads
        # and arbitrary response headers never enter the diagnostic record.
        parts, usage, finish = [], None, None
        done = False
        try:
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                diagnostics.setdefault("first_event_ms", elapsed())
                if payload == "[DONE]":
                    done = True
                    break
                event = json.loads(payload)
                if event.get("error"):
                    raise ModelError(CODE_BAD_RESPONSE, "模型流式响应返回错误")
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                for choice in event.get("choices") or []:
                    if choice.get("index", 0) != 0:
                        continue
                    finish = choice.get("finish_reason") or finish
                    content = (choice.get("delta") or {}).get("content")
                    if isinstance(content, str) and content:
                        diagnostics.setdefault("first_content_ms", elapsed())
                        parts.append(content)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ModelError(CODE_BAD_RESPONSE, "模型流式响应格式不完整") from exc
        diagnostics["finish_reason"] = finish if finish in ("stop", "length", None) else "other"
        if not done or finish is None:
            raise ModelError(CODE_BAD_RESPONSE, "模型流式响应中断，结果未完成", retryable=True)
        if finish not in ("stop", "length"):
            raise ModelError(CODE_BAD_RESPONSE, "模型流式响应未正常结束")
        return {"choices": [{"message": {"content": "".join(parts)}, "finish_reason": finish}], "usage": usage}

    def test_connection(
        self,
        *,
        model: str,
        supports_vision: bool,
        image_data_url: str | None = None,
        prompt: str = "回复：ok",
    ) -> CallResult:
        """测试连接级别调用。若提供图像但模型不支持看图，直接返回 VISION_UNSUPPORTED。"""
        if image_data_url and not supports_vision:
            raise ModelError(CODE_VISION_UNSUPPORTED, "模型不支持看图")
        content: list[dict[str, Any]] | str
        if image_data_url:
            content = [
                {"type": "image_url", "image_url": {"url": image_data_url, "detail": "low"}},
                {"type": "text", "text": prompt},
            ]
        else:
            content = prompt
        messages = [{"role": "user", "content": content}]
        try:
            return self.chat_completions(model=model, messages=messages, max_tokens=64)
        except ModelError:
            raise

    def _map_http_error(self, response: httpx.Response) -> ModelError | None:
        if response.status_code < 400:
            return None
        body = _safe_error_body(response.text)
        lower = body.lower()
        if response.status_code in {401, 403}:
            return ModelError(CODE_AUTH_INVALID, "密钥无效或无权限", http_status=response.status_code)
        if response.status_code == 429:
            return ModelError(CODE_RATE_LIMITED, "限流", retryable=True, http_status=429)
        if response.status_code == 402 or "balance" in lower or "insufficient" in lower or "余额" in body:
            return ModelError(CODE_BALANCE_INSUFFICIENT, "余额不足", http_status=response.status_code)
        if ("vision" in lower and "support" in lower) or ("image" in lower and "not support" in lower):
            return ModelError(CODE_VISION_UNSUPPORTED, "不支持看图", http_status=response.status_code)
        code = other_http_code(response.status_code)
        if response.status_code >= 500:
            return ModelError(
                code,
                f"服务端错误 {response.status_code}",
                retryable=True,
                http_status=response.status_code,
            )
        # 不把响应全文带进异常；码中保留状态码（M6）
        return ModelError(code, f"HTTP {response.status_code}", http_status=response.status_code)

    def _parse_success(self, data: dict[str, Any], *, model: str, status: int, latency_ms: int) -> CallResult:
        try:
            choices = data["choices"]
            message = choices[0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
            raw_content = message.get("content")
            if isinstance(raw_content, str):
                content = raw_content
                content_type = "text"
            elif isinstance(raw_content, list):
                # Content arrays are presentation wrappers, not the answer JSON.
                # Never mix reasoning/refusal/image parts into authoritative facts.
                content = "".join(part["text"] for part in raw_content
                    if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
                    and isinstance(part.get("text"), str))
                content_type = "parts"
            else:
                content = ""
                content_type = "none" if raw_content is None else "unsupported"
            finish = choices[0].get("finish_reason")
            reasoning = message.get("reasoning_content")
            reasoning_length = len(reasoning) if isinstance(reasoning, str) else 0
            diagnostics = {"content_length":len(content), "content_type":content_type,
                "reasoning_length":reasoning_length, "has_reasoning":bool(reasoning),
                "empty_final":not content.strip(), "truncated":finish == "length",
                "finish_reason":finish if finish in {None, "stop", "length", "tool_calls", "function_call", "content_filter"} else "other"}
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(CODE_BAD_RESPONSE, "返回格式不对") from exc

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        in_tok = out_tok = None
        if usage is not None:
            try:
                in_tok = int(usage.get("prompt_tokens"))
                out_tok = int(usage.get("completion_tokens"))
            except (TypeError, ValueError):
                in_tok = out_tok = None

        return CallResult(
            content=content,
            input_tokens=in_tok,
            output_tokens=out_tok,
            raw_status=status,
            model=model,
            provider=self.provider,
            latency_ms=latency_ms,
            finish_reason=finish,
            extras=diagnostics,
        )
