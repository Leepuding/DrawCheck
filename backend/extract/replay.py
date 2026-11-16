"""DC-05 Mock 回放：按审查图 SHA，回退 pdf_sha256:页序。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("drawcheck.extract.replay")

DEFAULT_REPLAY_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "synthetic"


class ReplayLookup:
    """加载 replay/<suite>/index.json 与 answers.json catalog。"""

    def __init__(self, root: Path | str | None = None, *, suite: str = "A"):
        self.root = Path(root) if root else DEFAULT_REPLAY_ROOT
        self.suite = suite
        self._index: dict[str, Any] | None = None
        self._answers: dict[str, Any] | None = None
        self._by_sha: dict[str, dict] = {}
        self._by_fallback: dict[str, dict] = {}

    def load(self) -> None:
        index_path = self.root / "replay" / self.suite / "index.json"
        answers_path = self.root / "answers.json"
        self._index = json.loads(index_path.read_text(encoding="utf-8"))
        self._answers = json.loads(answers_path.read_text(encoding="utf-8"))
        self._by_sha.clear()
        self._by_fallback.clear()
        for entry in self._index.get("entries") or []:
            sha = entry.get("review_png_sha256")
            if sha:
                self._by_sha[sha] = entry
            fb = entry.get("fallback_key") or (
                f"{entry.get('pdf_sha256')}:{entry.get('page')}"
                if entry.get("pdf_sha256") and entry.get("page") is not None
                else None
            )
            if fb:
                self._by_fallback[fb] = entry

    @property
    def poppler_version(self) -> str | None:
        if self._index is None:
            self.load()
        assert self._index is not None
        return self._index.get("poppler_version")

    def lookup(
        self,
        *,
        review_png_sha256: str | None = None,
        fallback_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        """返回 (entry, match_kind, warning)。match_kind: primary|fallback|miss。"""
        if self._index is None:
            self.load()
        if review_png_sha256 and review_png_sha256 in self._by_sha:
            return self._by_sha[review_png_sha256], "primary", None
        if fallback_key and fallback_key in self._by_fallback:
            warning = (
                f"审查图 SHA 未命中 replay 主键，已回退备用键 {fallback_key}；"
                f"可能因 Poppler 版本漂移（索引记录：{self.poppler_version}）"
            )
            logger.warning(warning)
            return self._by_fallback[fallback_key], "fallback", warning
        return None, "miss", None

    def catalog_rows_for_file(self, file_field: str) -> list[dict[str, Any]]:
        """按 answers.json catalog.<suite> 中 file 字段匹配目录行。"""
        if self._answers is None:
            self.load()
        assert self._answers is not None
        catalog = (self._answers.get("catalog") or {}).get(self.suite) or []
        base = file_field.replace("\\", "/")
        if base.startswith("build/"):
            base = base[len("build/") :]
        rows: list[dict[str, Any]] = []
        for row in catalog:
            rf = str(row.get("file") or "").replace("\\", "/")
            if rf == base:
                rows.append(dict(row))
        return rows

    def build_mock_payload(self, entry: dict[str, Any]) -> dict[str, Any]:
        """由 replay entry + answers catalog 组装校验前 payload。"""
        file_field = entry.get("file") or ""
        title_blocks = []
        for row in self.catalog_rows_for_file(file_field):
            title_blocks.append({
                "specialty": row.get("specialty") or "",
                "drawing_number": row.get("drawing_number") or "",
                "name": row.get("name") or "",
                "bbox": {"x0": 0.72, "y0": 0.78, "x1": 0.98, "y1": 0.98},
                "confidence": 0.99,
                "unclear": False,
                "no_title_block": bool(row.get("no_title_block")),
                "multi_frame": bool(row.get("multi_frame")),
                "out_of_scope": bool(row.get("out_of_scope")),
                "unlisted_in_catalog_sheet": bool(row.get("unlisted_in_catalog_sheet")),
                "file": row.get("file"),
                "page": row.get("page"),
                "page_size": row.get("page_size"),
            })
        facts = [dict(fact) for fact in (entry.get("facts") or [])]
        return {
            "prompt_version": None,
            "title_blocks": title_blocks,
            "facts": facts,
            "replay_file": file_field,
            "replay_page": entry.get("page"),
            "pdf_sha256": entry.get("pdf_sha256"),
        }
