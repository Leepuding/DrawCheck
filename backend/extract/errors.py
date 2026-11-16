"""看图识别错误。"""
from __future__ import annotations


class ExtractError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        self.model_failure: dict | None = None
        super().__init__(self.message)

    def as_error(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


CODE_BAD_FORMAT = "EXTRACT_BAD_FORMAT"
CODE_INCOMPLETE = "EXTRACT_INCOMPLETE"
CODE_NO_IMAGE = "EXTRACT_NO_IMAGE"
CODE_CACHE = "EXTRACT_CACHE_ERROR"
CODE_REPLAY_MISS = "EXTRACT_REPLAY_MISS"
