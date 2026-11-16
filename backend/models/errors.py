"""模型调用错误：对外只暴露错误码，异常信息脱敏。"""
from __future__ import annotations


class ModelError(Exception):
    """适配器与 Mock 抛出的统一错误。"""

    def __init__(self, code: str, message: str = "", *, retryable: bool = False, http_status: int | None = None):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status
        self.diagnostics: dict = {}
        super().__init__(f"{code}: {message}" if message else code)

    def __repr__(self) -> str:
        return f"ModelError(code={self.code!r}, retryable={self.retryable})"


# 与 SPEC 第 12 条失败文案表对应的内部错误码（界面文案由 DC-14 映射）
CODE_OK = "CONNECTION_OK"
CODE_CREDENTIAL_REQUIRED = "CREDENTIAL_REQUIRED"
CODE_AUTH_INVALID = "AUTH_INVALID"
CODE_BALANCE_INSUFFICIENT = "BALANCE_INSUFFICIENT"
CODE_RATE_LIMITED = "RATE_LIMITED"
CODE_TIMEOUT = "TIMEOUT"
CODE_CERTIFICATE_ERROR = "CERTIFICATE_ERROR"
CODE_VISION_UNSUPPORTED = "VISION_UNSUPPORTED"
CODE_LOCAL_UNREACHABLE = "LOCAL_UNREACHABLE"
CODE_BAD_RESPONSE = "BAD_RESPONSE"
CODE_PROXY_ERROR = "PROXY_ERROR"
CODE_OTHER = "OTHER"
CODE_LOW_CONFIDENCE = "LOW_CONFIDENCE"


def other_http_code(status: int) -> str:
    """M6：保留 HTTP 状态码，便于排查；不含响应体。"""
    return f"OTHER_HTTP_{int(status)}"


def other_net_code(exc: BaseException) -> str:
    """M6：保留网络异常类型名（去敏）。"""
    name = type(exc).__name__
    # 只保留标识符字符，避免异常消息泄漏进码
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name) or "Error"
    return f"OTHER_NET_{safe}"
