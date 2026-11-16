"""DC-11 自动关联：依据图签/目录引用/详图索引/共有编号；禁止仅凭同名或相同数字。"""
from .service import (
    LINK_KIND_CATALOG_REF,
    LINK_KIND_DETAIL_INDEX,
    LINK_KIND_SHARED_NUMBER,
    LINK_KIND_TITLEBLOCK,
    LinkingService,
    SameNameAmbiguity,
)

__all__ = [
    "LinkingService",
    "SameNameAmbiguity",
    "LINK_KIND_TITLEBLOCK",
    "LINK_KIND_CATALOG_REF",
    "LINK_KIND_DETAIL_INDEX",
    "LINK_KIND_SHARED_NUMBER",
]
