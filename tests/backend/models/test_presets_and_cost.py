from __future__ import annotations

from backend.models.cost import estimate_cost_micros
from backend.models.presets import (
    PROVIDER_ORDER,
    get_preset,
    list_presets,
    price_for_model,
    price_for_slot,
    same_base_model,
)
from backend.settings.codes import cost_warning_message, format_yuan_amount


def test_siliconflow_first_and_providers():
    presets = list_presets()
    assert [p["id"] for p in presets] == list(PROVIDER_ORDER)
    assert presets[0]["id"] == "siliconflow"
    assert presets[0]["display_name"] == "硅基流动"
    assert presets[0]["base_url"] == "https://api.siliconflow.cn/v1"
    assert presets[0]["default_primary_model"] != presets[0]["default_review_model"]
    assert presets[0]["measured"] is True
    assert "custom" in PROVIDER_ORDER
    for p in presets[1:]:
        if p["id"] != "custom":
            # custom 也是未实测
            pass
        assert p["measured"] is False or p["id"] == "siliconflow"


def test_same_base_model_cross_provider():
    assert same_base_model(
        "siliconflow", "zai-org/GLM-4.5V", "https://api.siliconflow.cn/v1",
        "zhipu", "glm-4.5v", "https://open.bigmodel.cn/api/paas/v4",
    )
    assert not same_base_model(
        "siliconflow", "Qwen/Qwen3-VL-32B-Instruct", "",
        "siliconflow", "zai-org/GLM-4.5V", "",
    )


def test_cost_estimate_micros_and_unknown():
    est = estimate_cost_micros(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        input_price_per_m_yuan=1.0,
        output_price_per_m_yuan=4.0,
    )
    assert est.estimated_cost_micros == 5_000_000
    assert est.estimated is True
    unknown = estimate_cost_micros(
        input_tokens=None,
        output_tokens=10,
        input_price_per_m_yuan=1.0,
        output_price_per_m_yuan=4.0,
    )
    assert unknown.token_unknown is True
    assert unknown.estimated_cost_micros is None
    assert "未知" in unknown.note


def test_price_by_model_not_role():
    """M3：单价按型号；互换主/复核后费用正确。"""
    glm = price_for_model("siliconflow", "zai-org/GLM-4.5V")
    q32 = price_for_model("siliconflow", "Qwen/Qwen3-VL-32B-Instruct")
    q8 = price_for_model("siliconflow", "Qwen/Qwen3-VL-8B-Instruct")
    q30 = price_for_model("siliconflow", "Qwen/Qwen3-VL-30B-A3B-Instruct")
    assert glm.input_per_m_yuan == 1.0 and glm.output_per_m_yuan == 6.0
    assert q32.input_per_m_yuan == 1.0 and q32.output_per_m_yuan == 4.0
    assert q8.input_per_m_yuan == 0.5 and q8.output_per_m_yuan == 2.0
    assert q30.input_per_m_yuan == 0.7 and q30.output_per_m_yuan == 2.8
    # 互换：主槽位用 GLM，复核用 32B —— 按型号而非角色
    assert price_for_slot("siliconflow", "primary", "zai-org/GLM-4.5V").output_per_m_yuan == 6.0
    assert price_for_slot("siliconflow", "review", "Qwen/Qwen3-VL-32B-Instruct").output_per_m_yuan == 4.0
    unknown = price_for_model("siliconflow", "not-a-real-model")
    assert unknown.known is False
    assert "未知" in unknown.image_billing_note or unknown.input_per_m_yuan == 0.0


def test_format_yuan_amount_m10_rules():
    """M10：金额显示格式（数字+空格+元 / 约 / 千分位 / 四舍五入 / 禁¥与科学计数）。"""
    # 0 / 不足 0.01 / 刚好 0.01
    assert format_yuan_amount(0) == "0.00 元"
    assert format_yuan_amount(8.5e-05) == "不足 0.01 元"
    assert format_yuan_amount(0.009999) == "不足 0.01 元"
    assert format_yuan_amount(0.01) == "0.01 元"
    # 2 位小数四舍五入
    assert format_yuan_amount(1.234567) == "1.23 元"
    assert format_yuan_amount(3.255) == "3.26 元"
    assert format_yuan_amount(999.995) == "1,000.00 元"
    # 千分位
    assert format_yuan_amount(1000) == "1,000.00 元"
    assert format_yuan_amount(1234.56) == "1,234.56 元"
    # 估算：大于 0 加「约」；0 / 不足不加；上限/plain 不加（N16）
    assert format_yuan_amount(3.25, kind="estimate") == "约 3.25 元"
    assert format_yuan_amount(0, kind="estimate") == "0.00 元"
    assert "约" not in format_yuan_amount(0, kind="estimate")
    assert format_yuan_amount(8.5e-05, kind="estimate") == "不足 0.01 元"
    assert "约" not in format_yuan_amount(8.5e-05, kind="estimate")
    assert format_yuan_amount(50, kind="limit") == "50.00 元"
    assert not format_yuan_amount(50, kind="limit").startswith("约")
    assert format_yuan_amount(100, kind="plain") == "100.00 元"
    # 禁 ¥、禁科学计数
    for v in (0, 8.5e-05, 1.2e-4, 1.234567, 100, 1000, 999.995, -1):
        s = format_yuan_amount(v)
        assert "¥" not in s
        assert "e" not in s.lower() or s == "不足 0.01 元"
        assert s.endswith("元")
    # 负数 → 0.00 元（并记日志，由 caplog 用例另测）
    assert format_yuan_amount(-1) == "0.00 元"
    assert format_yuan_amount(-0.5, kind="estimate") == "0.00 元"
    assert "约" not in format_yuan_amount(-0.5, kind="estimate")


def test_cost_warning_message_m10_assembly():
    """M10：80% 示例文案拼装；不足 0.01 整句不加「约」。"""
    assert cost_warning_message(40, 50) == (
        "本月模型费用估算已用 80%（约 40.00 元 / 上限 50.00 元）"
    )
    msg_tiny = cost_warning_message(8e-5, 50)
    assert msg_tiny == "本月模型费用估算已用 80%（不足 0.01 元 / 上限 50.00 元）"
    assert "约 不足" not in msg_tiny
    assert "¥" not in msg_tiny


def test_local_requires_address_flag():
    assert get_preset("local").requires_base_url is True
    assert get_preset("custom").requires_base_url is True
