"""服务商预设注册表。硅基流动排第一；单价来源见 NOTES-DC-08。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

# 查阅日期（Asia/Shanghai）：2026-11-03
SILICONFLOW_DOCS_QUICKSTART = "https://docs.siliconflow.cn/cn/userguide/quickstart"
SILICONFLOW_DOCS_VISION = "https://docs.siliconflow.com/en/userguide/capabilities/vision"
SILICONFLOW_MODELS_PAGE = "https://siliconflow.cn/models?type=multimodal"
SILICONFLOW_PRICING_CN = "https://www.siliconflow.cn/pricing"
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
PRICE_LOOKUP_AT = "2026-11-03 Asia/Shanghai"

# 预设官方域名白名单（M8：密钥只发往该预设官方域名）
PRESET_ALLOWED_HOSTS: dict[str, frozenset[str]] = {
    "siliconflow": frozenset({"api.siliconflow.cn"}),
    "qwen": frozenset({"dashscope.aliyuncs.com"}),
    "deepseek": frozenset({"api.deepseek.com"}),
    "zhipu": frozenset({"open.bigmodel.cn"}),
}


@dataclass(frozen=True)
class ModelPrice:
    input_per_m_yuan: float
    output_per_m_yuan: float
    image_billing_note: str
    source_url: str
    looked_up_at: str
    known: bool = True  # False = 单价未知


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    display_name: str
    base_url: str
    default_primary_model: str
    default_review_model: str
    supports_vision: bool
    requires_base_url: bool
    measured: bool  # False = 未实测
    price_primary: ModelPrice  # 兼容旧字段：等于默认主型号单价
    price_review: ModelPrice  # 兼容旧字段：等于默认复核型号单价
    source_url: str
    looked_up_at: str
    # 可选：该服务商下模型 id → 底座标识（用于跨服务商「同一模型」判定）
    base_model_ids: dict[str, str] = field(default_factory=dict)
    locks_base_url: bool = True  # 预设服务商禁止改地址（须改「自定义」）

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def _price(inp: float, out: float, note: str, url: str, *, known: bool = True) -> ModelPrice:
    return ModelPrice(inp, out, note, url, PRICE_LOOKUP_AT, known=known)


_SF_VISION_NOTE = (
    "图像按官方视觉计费折算为 input tokens 后与文本一并按输入单价计费；"
    "详见 SiliconFlow Vision 文档。"
)

# 按型号取单价（M3）。来源：https://www.siliconflow.cn/pricing 查阅 2026-11-03 Asia/Shanghai
# 页面字段：inputPrice + 列表展示 price（元 / 百万 token）；查不到则 known=False。
SILICONFLOW_MODEL_PRICES: dict[str, ModelPrice] = {
    "Qwen/Qwen3-VL-32B-Instruct": _price(1.0, 4.0, _SF_VISION_NOTE, SILICONFLOW_PRICING_CN),
    "zai-org/GLM-4.5V": _price(1.0, 6.0, _SF_VISION_NOTE, SILICONFLOW_PRICING_CN),
    "Qwen/Qwen3-VL-8B-Instruct": _price(0.5, 2.0, _SF_VISION_NOTE, SILICONFLOW_PRICING_CN),
    "Qwen/Qwen3-VL-30B-A3B-Instruct": _price(0.7, 2.8, _SF_VISION_NOTE, SILICONFLOW_PRICING_CN),
}

# 跨服务商同一底座：key 为规范化后的 model 字符串（小写、去空白）
# 值相同则视为同一模型（裁定第 11 条）。认不出的组合按不同模型处理。
BASE_MODEL_ALIASES: dict[str, str] = {
    "qwen/qwen3-vl-32b-instruct": "qwen3-vl-32b-instruct",
    "qwen3-vl-32b-instruct": "qwen3-vl-32b-instruct",
    "qwen/qwen3-vl-8b-instruct": "qwen3-vl-8b-instruct",
    "qwen3-vl-8b-instruct": "qwen3-vl-8b-instruct",
    "qwen/qwen3-vl-30b-a3b-instruct": "qwen3-vl-30b-a3b-instruct",
    "qwen3-vl-30b-a3b-instruct": "qwen3-vl-30b-a3b-instruct",
    "zai-org/glm-4.5v": "glm-4.5v",
    "glm-4.5v": "glm-4.5v",
    "glm4.5v": "glm-4.5v",
}


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "siliconflow": ProviderPreset(
        id="siliconflow",
        display_name="硅基流动",
        base_url=SILICONFLOW_BASE_URL,
        # 工程定稿（R2）：主 GLM-4.5V，复核 Qwen3-VL-32B；备选复核 30B-A3B（见 NOTES）
        default_primary_model="zai-org/GLM-4.5V",
        default_review_model="Qwen/Qwen3-VL-32B-Instruct",
        supports_vision=True,
        requires_base_url=False,
        measured=True,
        price_primary=SILICONFLOW_MODEL_PRICES["zai-org/GLM-4.5V"],
        price_review=SILICONFLOW_MODEL_PRICES["Qwen/Qwen3-VL-32B-Instruct"],
        source_url=SILICONFLOW_DOCS_QUICKSTART,
        looked_up_at=PRICE_LOOKUP_AT,
        base_model_ids={
            "Qwen/Qwen3-VL-32B-Instruct": "qwen3-vl-32b-instruct",
            "zai-org/GLM-4.5V": "glm-4.5v",
            "Qwen/Qwen3-VL-8B-Instruct": "qwen3-vl-8b-instruct",
            "Qwen/Qwen3-VL-30B-A3B-Instruct": "qwen3-vl-30b-a3b-instruct",
        },
        locks_base_url=True,
    ),
    "qwen": ProviderPreset(
        id="qwen",
        display_name="通义千问",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_primary_model="qwen-vl-max",
        default_review_model="qwen-vl-plus",
        supports_vision=True,
        requires_base_url=False,
        measured=False,
        price_primary=_price(0.0, 0.0, "未实测，单价占位 0；上线前须按官方更新", "", known=False),
        price_review=_price(0.0, 0.0, "未实测，单价占位 0；上线前须按官方更新", "", known=False),
        source_url="",
        looked_up_at=PRICE_LOOKUP_AT,
        base_model_ids={"qwen-vl-max": "qwen-vl-max", "qwen-vl-plus": "qwen-vl-plus"},
        locks_base_url=True,
    ),
    "deepseek": ProviderPreset(
        id="deepseek",
        display_name="深度求索",
        base_url="https://api.deepseek.com/v1",
        default_primary_model="deepseek-chat",
        default_review_model="deepseek-reasoner",
        supports_vision=False,
        requires_base_url=False,
        measured=False,
        price_primary=_price(0.0, 0.0, "未实测；当前默认型号不支持看图", "", known=False),
        price_review=_price(0.0, 0.0, "未实测；当前默认型号不支持看图", "", known=False),
        source_url="",
        looked_up_at=PRICE_LOOKUP_AT,
        locks_base_url=True,
    ),
    "zhipu": ProviderPreset(
        id="zhipu",
        display_name="智谱清言",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_primary_model="glm-4v",
        default_review_model="glm-4.5v",
        supports_vision=True,
        requires_base_url=False,
        measured=False,
        price_primary=_price(0.0, 0.0, "未实测，单价占位 0", "", known=False),
        price_review=_price(0.0, 0.0, "未实测，单价占位 0", "", known=False),
        source_url="",
        looked_up_at=PRICE_LOOKUP_AT,
        base_model_ids={"glm-4v": "glm-4v", "glm-4.5v": "glm-4.5v", "GLM-4.5V": "glm-4.5v"},
        locks_base_url=True,
    ),
    "local": ProviderPreset(
        id="local",
        display_name="本地模型",
        base_url="",
        default_primary_model="local-vl",
        default_review_model="local-vl-review",
        supports_vision=True,
        requires_base_url=True,
        measured=False,
        price_primary=_price(0.0, 0.0, "本地不计云费用；估算记 0", ""),
        price_review=_price(0.0, 0.0, "本地不计云费用；估算记 0", ""),
        source_url="",
        looked_up_at=PRICE_LOOKUP_AT,
        locks_base_url=False,
    ),
    "custom": ProviderPreset(
        id="custom",
        display_name="自定义",
        base_url="",
        default_primary_model="",
        default_review_model="",
        supports_vision=True,
        requires_base_url=True,
        measured=False,
        price_primary=_price(0.0, 0.0, "自定义服务商单价未知", "", known=False),
        price_review=_price(0.0, 0.0, "自定义服务商单价未知", "", known=False),
        source_url="",
        looked_up_at=PRICE_LOOKUP_AT,
        locks_base_url=False,
    ),
}

# 下拉顺序：硅基流动第一，其余按 Q-03 原序，自定义殿后
PROVIDER_ORDER = ("siliconflow", "qwen", "deepseek", "zhipu", "local", "custom")


def list_presets() -> list[dict[str, Any]]:
    return [deepcopy(PROVIDER_PRESETS[pid].public_dict()) for pid in PROVIDER_ORDER]


def get_preset(provider_id: str) -> ProviderPreset | None:
    return PROVIDER_PRESETS.get(provider_id)


def normalize_base_url(url: str | None) -> str:
    if not url:
        return ""
    return url.rstrip("/")


def host_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
    except Exception:
        return ""


def is_preset_url_allowed(provider: str, base_url: str | None) -> bool:
    """预设服务商的地址是否仍指向官方域名（M8）。"""
    preset = get_preset(provider)
    if preset is None:
        return False
    if not preset.locks_base_url:
        return True
    allowed = PRESET_ALLOWED_HOSTS.get(provider)
    if not allowed:
        # 无白名单时与预设 base_url 精确比较
        return normalize_base_url(base_url) == normalize_base_url(preset.base_url)
    host = host_of(base_url or preset.base_url)
    return host in allowed


def resolve_base_model_id(provider: str, model: str) -> str | None:
    """返回底座标识；认不出则 None（按不同模型处理）。"""
    preset = get_preset(provider)
    if preset and model in preset.base_model_ids:
        return preset.base_model_ids[model]
    key = (model or "").strip().lower()
    if key in BASE_MODEL_ALIASES:
        return BASE_MODEL_ALIASES[key]
    # 去掉常见厂商前缀后再查
    if "/" in key:
        short = key.split("/", 1)[1]
        if short in BASE_MODEL_ALIASES:
            return BASE_MODEL_ALIASES[short]
        return BASE_MODEL_ALIASES.get(key)
    return BASE_MODEL_ALIASES.get(key)


def same_base_model(
    provider_a: str,
    model_a: str,
    base_url_a: str | None,
    provider_b: str,
    model_b: str,
    base_url_b: str | None,
) -> bool:
    """主≠复核判定：同 provider+model(+base_url) 或跨服务商同一底座。"""
    if not model_a or not model_b:
        return False
    if (
        provider_a == provider_b
        and model_a == model_b
        and normalize_base_url(base_url_a) == normalize_base_url(base_url_b)
    ):
        return True
    base_a = resolve_base_model_id(provider_a, model_a)
    base_b = resolve_base_model_id(provider_b, model_b)
    if base_a is None or base_b is None:
        return False
    return base_a == base_b


def price_for_model(provider: str, model: str | None) -> ModelPrice:
    """按型号取单价（M3）。查不到标「估算（单价未知）」。"""
    if provider == "siliconflow" and model and model in SILICONFLOW_MODEL_PRICES:
        return SILICONFLOW_MODEL_PRICES[model]
    # 兼容：去掉空白后再查
    if provider == "siliconflow" and model:
        for key, price in SILICONFLOW_MODEL_PRICES.items():
            if key.lower() == model.strip().lower():
                return price
    preset = get_preset(provider)
    if preset is None:
        return _price(0.0, 0.0, "估算（单价未知）", "", known=False)
    # 本地 0；其余未知
    if provider == "local":
        return preset.price_primary
    return _price(0.0, 0.0, "估算（单价未知）", "", known=False)


def price_for_slot(provider: str, role: str, model: str | None = None) -> ModelPrice:
    """兼容旧签名；优先按型号，缺型号时回退默认主/复核型号。"""
    if model:
        return price_for_model(provider, model)
    preset = get_preset(provider)
    if preset is None:
        return _price(0.0, 0.0, "估算（单价未知）", "", known=False)
    fallback = preset.default_primary_model if role == "primary" else preset.default_review_model
    return price_for_model(provider, fallback)


def default_slot(provider: str, role: str) -> dict[str, str]:
    preset = get_preset(provider)
    if preset is None:
        raise KeyError(provider)
    model = preset.default_primary_model if role == "primary" else preset.default_review_model
    return {
        "provider": provider,
        "model": model,
        "base_url": preset.base_url,
        "credential_ref": "",
    }
