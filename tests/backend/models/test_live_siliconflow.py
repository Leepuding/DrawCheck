from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import pytest
from PIL import Image

from backend.models.cost import MICROS_PER_YUAN, estimate_cost_micros
from backend.models.openai_compat import OpenAICompatClient, SecretStr
from backend.models.presets import get_preset, price_for_model
from backend.settings.ledger import CallLedger

LIVE = os.environ.get("DRAWCHECK_LIVE_MODEL") == "1" and bool(os.environ.get("SILICONFLOW_API_KEY"))


def _tiny_png_data_url() -> str:
    img = Image.new("RGB", (32, 32), color=(180, 180, 180))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="需 DRAWCHECK_LIVE_MODEL=1 且存在 SILICONFLOW_API_KEY")
def test_live_primary_and_review_with_image():
    """真调用只到测试连接：主/复核各 ≥1，含 ≥1 图像。台账写入证据目录。"""
    evidence = Path("/workspace/notes/REQ-DRAWCHECK/DC-08")
    evidence.mkdir(parents=True, exist_ok=True)
    ledger = CallLedger(evidence / "live-calls.json")

    preset = get_preset("siliconflow")
    assert preset is not None
    client = OpenAICompatClient(
        provider="siliconflow",
        base_url=preset.base_url,
        api_key=SecretStr(os.environ["SILICONFLOW_API_KEY"]),
        timeout_sec=180,
        max_retries=1,
        retry_backoff_sec=2.0,
    )

    primary = client.test_connection(
        model=preset.default_primary_model,
        supports_vision=True,
        prompt="只回复一个字：好",
    )
    price_p = price_for_model("siliconflow", preset.default_primary_model)
    cost_p = estimate_cost_micros(
        input_tokens=primary.input_tokens,
        output_tokens=primary.output_tokens,
        input_price_per_m_yuan=price_p.input_per_m_yuan,
        output_price_per_m_yuan=price_p.output_per_m_yuan,
    )
    ledger.add({
        "model": preset.default_primary_model,
        "role": "primary",
        "purpose": "test_connection",
        "provider": "siliconflow",
        "input_tokens": primary.input_tokens,
        "output_tokens": primary.output_tokens,
        "estimated_cost_micros": cost_p.estimated_cost_micros,
        "estimated_cost_yuan": None if cost_p.estimated_cost_micros is None else cost_p.estimated_cost_micros / MICROS_PER_YUAN,
        "status": "ok",
        "code": "CONNECTION_OK",
        "image": False,
    })

    review = client.test_connection(
        model=preset.default_review_model,
        supports_vision=True,
        image_data_url=_tiny_png_data_url(),
        prompt="只答：有",
    )
    price_r = price_for_model("siliconflow", preset.default_review_model)
    cost_r = estimate_cost_micros(
        input_tokens=review.input_tokens,
        output_tokens=review.output_tokens,
        input_price_per_m_yuan=price_r.input_per_m_yuan,
        output_price_per_m_yuan=price_r.output_per_m_yuan,
    )
    ledger.add({
        "model": preset.default_review_model,
        "role": "review",
        "purpose": "test_connection",
        "provider": "siliconflow",
        "input_tokens": review.input_tokens,
        "output_tokens": review.output_tokens,
        "estimated_cost_micros": cost_r.estimated_cost_micros,
        "estimated_cost_yuan": None if cost_r.estimated_cost_micros is None else cost_r.estimated_cost_micros / MICROS_PER_YUAN,
        "status": "ok",
        "code": "CONNECTION_OK",
        "image": True,
    })

    assert primary.content is not None
    assert review.content is not None
    assert "SILICONFLOW" not in repr(primary)
