"""目录服务错误。"""
from __future__ import annotations


class CatalogError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)

    def as_error(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}
