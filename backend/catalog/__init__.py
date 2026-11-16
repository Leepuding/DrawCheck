"""DC-09 自动目录与改名（人工修正）。"""
from .errors import CatalogError
from .service import CatalogService, MANUAL_CORRECTION_LABEL

__all__ = ["CatalogService", "CatalogError", "MANUAL_CORRECTION_LABEL"]
