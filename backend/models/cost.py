"""按预设单价估算费用；统一整数微元（1 元 = 1_000_000 微元）。"""
from __future__ import annotations

from dataclasses import dataclass

MICROS_PER_YUAN = 1_000_000


@dataclass(frozen=True)
class CostEstimate:
    estimated_cost_micros: int | None
    estimated: bool  # 对外一律标「估算」
    token_unknown: bool
    note: str

    def as_dict(self) -> dict:
        yuan = None if self.estimated_cost_micros is None else self.estimated_cost_micros / MICROS_PER_YUAN
        return {
            "estimated_cost_micros": self.estimated_cost_micros,
            "estimated_cost_yuan": yuan,
            "estimated": True,
            "token_unknown": self.token_unknown,
            "note": self.note,
        }


def estimate_cost_micros(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    input_price_per_m_yuan: float,
    output_price_per_m_yuan: float,
) -> CostEstimate:
    """token × 元/百万 token → 微元。缺 usage 时记 token 未知，不当作 0 元成功。"""
    if input_tokens is None or output_tokens is None:
        return CostEstimate(
            estimated_cost_micros=None,
            estimated=True,
            token_unknown=True,
            note="估算（token 未知）",
        )
    if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens < 0 or output_tokens < 0:
        return CostEstimate(None, True, True, "估算（token 无效）")
    # 用整数运算：先放大再除，避免浮点累加误差
    input_micros = int(round(input_tokens * input_price_per_m_yuan * MICROS_PER_YUAN / 1_000_000))
    output_micros = int(round(output_tokens * output_price_per_m_yuan * MICROS_PER_YUAN / 1_000_000))
    return CostEstimate(
        estimated_cost_micros=input_micros + output_micros,
        estimated=True,
        token_unknown=False,
        note="估算",
    )
