"""设置后端：模型配置、费用、容差、测试连接、前置检查。"""
from .codes import (
    MODEL_STATUS_DUAL,
    MODEL_STATUS_NONE,
    MODEL_STATUS_SINGLE,
    PRECHECK_COST_EXCEEDED,
    PRECHECK_COST_WARNING,
    PRECHECK_LIMIT_REQUIRED,
    PRECHECK_NO_MODEL,
    PRECHECK_OK,
)
from .credentials import CredentialService
from .service import SettingsService, SettingsError

__all__ = [
    "CredentialService",
    "SettingsError",
    "SettingsService",
    "MODEL_STATUS_DUAL",
    "MODEL_STATUS_NONE",
    "MODEL_STATUS_SINGLE",
    "PRECHECK_OK",
    "PRECHECK_NO_MODEL",
    "PRECHECK_LIMIT_REQUIRED",
    "PRECHECK_COST_EXCEEDED",
    "PRECHECK_COST_WARNING",
]
