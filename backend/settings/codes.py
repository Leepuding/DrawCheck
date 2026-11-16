"""设置与前置检查错误码（供 DC-10/14/17 读取）。"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

# 模型就绪状态
MODEL_STATUS_NONE = "none"
MODEL_STATUS_SINGLE = "single"
MODEL_STATUS_DUAL = "dual"

# 保存校验
ERR_LOCAL_URL_REQUIRED = "LOCAL_URL_REQUIRED"
ERR_PRIMARY_REQUIRED = "PRIMARY_REQUIRED"
ERR_SAME_MODEL = "SAME_MODEL"
ERR_MONTHLY_LIMIT_REQUIRED = "MONTHLY_LIMIT_REQUIRED"
ERR_INVALID_SETTINGS = "INVALID_SETTINGS"
ERR_CREDENTIAL_STORE = "CREDENTIAL_STORE_ERROR"
ERR_PRESET_URL_LOCKED = "PRESET_URL_LOCKED"

# 开始审查前置检查
PRECHECK_OK = "OK"
PRECHECK_NO_MODEL = "NO_MODEL"
PRECHECK_LIMIT_REQUIRED = "LIMIT_REQUIRED"
PRECHECK_COST_EXCEEDED = "COST_EXCEEDED"
PRECHECK_COST_WARNING = "COST_WARNING"

# 测试连接（与 models.errors 对齐，再导出一份方便设置层引用）
TEST_OK = "CONNECTION_OK"
TEST_CREDENTIAL_REQUIRED = "CREDENTIAL_REQUIRED"
TEST_STALE = "TEST_RESULT_STALE"

# 用户可见文案键（DC-14 用；后端可附带 message 供联调）
MESSAGES = {
    TEST_OK: "连接正常",
    TEST_CREDENTIAL_REQUIRED: "需先填写",
    "AUTH_INVALID": "密钥无效或没有权限，请检查后重新填写",
    "BALANCE_INSUFFICIENT": "服务商账户余额不足，请到服务商处充值",
    "RATE_LIMITED": "服务商暂时限制了调用次数，请稍后再试",
    "TIMEOUT": "连接超时，请检查网络后再试",
    "CERTIFICATE_ERROR": "无法建立安全连接，请检查网络或代理设置",
    "VISION_UNSUPPORTED": "这个模型不能看图，请换一个支持看图的模型",
    "LOCAL_UNREACHABLE": "连不上本地模型地址，请确认地址正确且本地模型已启动",
    ERR_PRIMARY_REQUIRED: "请先填主模型；只用一个模型时填在主模型里",
    ERR_SAME_MODEL: "主模型和复核模型是同一个模型，请换一个复核模型或清空复核",
    ERR_MONTHLY_LIMIT_REQUIRED: "请填写每月费用上限（元）",
    ERR_LOCAL_URL_REQUIRED: "本地模型须填写地址",
    ERR_PRESET_URL_LOCKED: "改服务商地址请改选「自定义」并重新填写密钥",
    PRECHECK_NO_MODEL: "先配置模型才能审查",
    PRECHECK_COST_EXCEEDED: "本月费用估算已到上限，审查已暂停。可在设置里调高上限后点「开始审查」继续。",
}


def format_yuan_amount(yuan: float, *, kind: str = "plain") -> str:
    """金额显示格式（SPEC · DC-08 补充裁定）。

    - 统一「数字 + 空格 + 元」，不用「¥」；禁科学计数法。
    - 2 位小数四舍五入；绝对值 ≥1000 加千分位逗号。
    - 大于 0 但不到 0.01：「不足 0.01 元」。
    - 负数按「0.00 元」显示并记日志。
    - kind="estimate"：大于 0 的估算值前加「约」；显示为「0.00 元」或「不足 0.01 元」时不加「约」；
      kind="limit"/"plain"：不加「约」。
    - 上限输入框原值由调用方保留；本函数只生成展示文案。
    """
    try:
        value = float(yuan)
    except (TypeError, ValueError):
        value = 0.0

    if value < 0:
        logging.getLogger(__name__).warning(
            "format_yuan_amount: 负数金额 %r 按 0.00 元显示", yuan
        )
        value = 0.0

    # 大于 0 但不到 0.01（四舍五入前按原值判定）
    if value > 0 and value < 0.01:
        return "不足 0.01 元"

    try:
        quantized = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        quantized = Decimal("0.00")

    # 四舍五入后若变为 0，但仍有极小正数已在上面处理；此处正常格式化
    abs_q = abs(quantized)
    if abs_q >= 1000:
        number = f"{quantized:,.2f}"
    else:
        number = f"{quantized:.2f}"

    text = f"{number} 元"
    # N16：估算值为 0（含负数归零后）显示「0.00 元」，不加「约」
    if kind == "estimate" and text != "0.00 元":
        return f"约 {text}"
    return text


def cost_warning_message(used_yuan: float, limit_yuan: float) -> str:
    used = format_yuan_amount(used_yuan, kind="estimate")
    limit = format_yuan_amount(limit_yuan, kind="limit")
    return f"本月模型费用估算已用 80%（{used} / 上限 {limit}）"


def other_failure_message(code: str) -> str:
    return f"连接失败（错误码：{code}），请稍后再试"
