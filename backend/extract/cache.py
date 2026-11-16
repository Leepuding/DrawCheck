"""按 image_sha256 + prompt_version + role 缓存识别结果。"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any


class ExtractCache:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.dir = self.root / "cache" / "extract"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, image_sha256: str, prompt_version: str, role: str) -> Path:
        key = hashlib.sha256(f"{image_sha256}|{prompt_version}|{role}".encode()).hexdigest()
        return self.dir / f"{key}.json"

    def get(self, image_sha256: str, prompt_version: str, role: str) -> dict[str, Any] | None:
        path = self._path(image_sha256, prompt_version, role)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def set(self, image_sha256: str, prompt_version: str, role: str, payload: dict[str, Any]) -> None:
        path = self._path(image_sha256, prompt_version, role)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
