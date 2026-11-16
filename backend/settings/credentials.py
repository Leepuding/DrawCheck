"""凭据服务：产品用 keyring；开发可读 env 引用；替身只存假密钥。"""
from __future__ import annotations

import os
import threading
from typing import Protocol

from backend.models.openai_compat import SecretStr
from backend.settings.codes import ERR_CREDENTIAL_STORE


class CredentialError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(message or code)


class CredentialBackend(Protocol):
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def get_password(self, service: str, username: str) -> str | None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class MemoryCredentialBackend:
    """box / 无 keyring 后端时的替身；调用方不得写入真密钥。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        with self._lock:
            self._data[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        with self._lock:
            return self._data.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        with self._lock:
            self._data.pop((service, username), None)


SERVICE_NAME = "DrawCheck"


def env_credential_allowed() -> bool:
    """产品模式默认不读环境变量密钥；仅开发/测试显式开关（M8）。"""
    return os.environ.get("DRAWCHECK_DEV") == "1" or os.environ.get("DRAWCHECK_LIVE_MODEL") == "1"


class CredentialService:
    def __init__(
        self,
        backend: CredentialBackend | None = None,
        *,
        allow_env: bool | None = None,
    ):
        # M8：默认跟显式开关；调用方可强制传入
        self.allow_env = env_credential_allowed() if allow_env is None else allow_env
        self.backend_name = "memory"
        if backend is not None:
            self.backend = backend
        else:
            self.backend = self._detect_backend()

    def _detect_backend(self) -> CredentialBackend:
        try:
            import keyring
            from keyring.errors import KeyringError  # noqa: F401

            kr = keyring.get_keyring()
            name = type(kr).__name__.lower()
            # fail / null / plain 等视为不可用
            if "fail" in name or "null" in name or "chainer" in name:
                # 再试一次实际读写
                probe_user = "__drawcheck_probe__"
                try:
                    kr.set_password(SERVICE_NAME, probe_user, "probe")
                    kr.delete_password(SERVICE_NAME, probe_user)
                    self.backend_name = type(kr).__name__
                    return kr  # type: ignore[return-value]
                except Exception:
                    self.backend_name = "memory"
                    return MemoryCredentialBackend()
            self.backend_name = type(kr).__name__
            return kr  # type: ignore[return-value]
        except Exception:
            self.backend_name = "memory"
            return MemoryCredentialBackend()

    def save_secret(self, ref: str, secret: str, *, allow_real: bool = False) -> str:
        """写入凭据。Memory 后端默认拒绝疑似真密钥（除非 allow_real 且非 memory）。"""
        if not ref or not isinstance(ref, str):
            raise CredentialError(ERR_CREDENTIAL_STORE, "无效凭据引用")
        if ref.startswith("env:"):
            raise CredentialError(ERR_CREDENTIAL_STORE, "环境变量引用只读")
        if isinstance(self.backend, MemoryCredentialBackend) and not allow_real:
            # 替身只允许假密钥：拒绝像 SiliconFlow 真钥的长 token（启发式）
            if secret and len(secret) >= 20 and not secret.startswith(("mock-", "test-", "fake-", "canary-")):
                raise CredentialError(ERR_CREDENTIAL_STORE, "内存替身不得存真密钥")
        try:
            self.backend.set_password(SERVICE_NAME, ref, secret)
        except Exception as exc:
            raise CredentialError(ERR_CREDENTIAL_STORE, "写入凭据失败") from exc
        return ref

    def resolve(self, credential_ref: str | None) -> SecretStr:
        if not credential_ref:
            return SecretStr("")
        if credential_ref.startswith("env:"):
            if not self.allow_env:
                return SecretStr("")
            env_name = credential_ref[4:]
            # 只允许白名单环境变量名，避免任意读取
            if env_name != "SILICONFLOW_API_KEY":
                raise CredentialError(ERR_CREDENTIAL_STORE, "不支持的环境变量引用")
            return SecretStr(os.environ.get(env_name, ""))
        try:
            value = self.backend.get_password(SERVICE_NAME, credential_ref)
        except Exception as exc:
            raise CredentialError(ERR_CREDENTIAL_STORE, "读取凭据失败") from exc
        return SecretStr(value or "")

    def delete(self, credential_ref: str) -> None:
        if not credential_ref or credential_ref.startswith("env:"):
            return
        try:
            self.backend.delete_password(SERVICE_NAME, credential_ref)
        except Exception:
            pass
