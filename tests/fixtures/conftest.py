"""DC-05 夹具测试公共路径。"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYN = ROOT / "fixtures" / "synthetic"


@pytest.fixture(scope="session")
def synthetic_root() -> Path:
    return SYN


@pytest.fixture(scope="session")
def ensure_generated(synthetic_root: Path) -> Path:
    """若 build 缺失则生成一次。"""
    marker = synthetic_root / "build" / "A" / "S-001-plan.pdf"
    if not marker.is_file():
        from tools.fixtures.generate import generate
        generate(synthetic_root)
    return synthetic_root
