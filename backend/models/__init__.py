"""模型接入层：OpenAI 兼容适配器、预设、Mock、费用估算。"""
from .cost import CostEstimate, estimate_cost_micros
from .errors import ModelError
from .mock import MockModelClient
from .openai_compat import OpenAICompatClient, CallResult
from .presets import PROVIDER_PRESETS, get_preset, list_presets, price_for_model, price_for_slot, same_base_model

__all__ = [
    "CostEstimate",
    "CallResult",
    "MockModelClient",
    "ModelError",
    "OpenAICompatClient",
    "PROVIDER_PRESETS",
    "estimate_cost_micros",
    "get_preset",
    "list_presets",
    "price_for_model",
    "price_for_slot",
    "same_base_model",
]
