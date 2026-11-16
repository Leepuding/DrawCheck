"""DC-11 规则 A：目录与图签、尺寸、标高；数值比较本地完成（uses_model=False）。"""
from .catalog_titleblock import run_catalog_titleblock
from .dimension import run_dimension
from .elevation import run_elevation
from .service import RulesAResult, RulesAService

__all__ = [
    "RulesAService",
    "RulesAResult",
    "run_catalog_titleblock",
    "run_dimension",
    "run_elevation",
]

from .rules_b import RulesBService, RulesBResult

from .registry import recalculate_for_pending
