"""测试连接用量台账（不入 model_calls；不含密钥与请求体）。"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


def _now_local() -> datetime:
    """本机时区当前时间（M2）。"""
    return datetime.now().astimezone()


class CallLedger:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._rows: list[dict[str, Any]] = []
        if path and path.exists():
            try:
                self._rows = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(self._rows, list):
                    self._rows = []
            except Exception:
                self._rows = []

    def add(self, row: dict[str, Any]) -> dict[str, Any]:
        clean = {
            "time": row.get("time") or _now_local().isoformat(timespec="seconds"),
            "model": row.get("model"),
            "role": row.get("role"),
            "purpose": row.get("purpose", "test_connection"),
            "provider": row.get("provider"),
            "input_tokens": row.get("input_tokens"),
            "output_tokens": row.get("output_tokens"),
            "estimated_cost_yuan": row.get("estimated_cost_yuan"),
            "estimated_cost_micros": row.get("estimated_cost_micros"),
            "status": row.get("status"),
            "code": row.get("code"),
            "image": bool(row.get("image")),
        }
        # 可选排查字段（去敏）：HTTP 状态 / 延迟等
        for extra in ("http_status", "latency_ms", "by", "note"):
            if extra in row and row[extra] is not None:
                clean[extra] = row[extra]
        with self._lock:
            self._rows.append(clean)
            self._persist()
        return clean

    def list_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._rows)

    def month_cost_micros(self, *, now: datetime | None = None) -> int:
        now = now or _now_local()
        total = 0
        with self._lock:
            for row in self._rows:
                if row.get("estimated_cost_micros") is None:
                    continue
                try:
                    ts = datetime.fromisoformat(row["time"])
                    if ts.tzinfo is None:
                        # 无时区时按「本机当前时区」解释
                        ts = ts.replace(tzinfo=now.tzinfo)
                    local = ts.astimezone(now.tzinfo)
                    if local.year == now.year and local.month == now.month:
                        total += int(row["estimated_cost_micros"])
                except Exception:
                    continue
        return total

    def summary(self) -> dict[str, Any]:
        rows = self.list_rows()
        tok_in = sum(r["input_tokens"] or 0 for r in rows if isinstance(r.get("input_tokens"), int))
        tok_out = sum(r["output_tokens"] or 0 for r in rows if isinstance(r.get("output_tokens"), int))
        micros = sum(r["estimated_cost_micros"] or 0 for r in rows if isinstance(r.get("estimated_cost_micros"), int))
        return {
            "count": len(rows),
            "input_tokens": tok_in,
            "output_tokens": tok_out,
            "estimated_cost_micros": micros,
            "estimated_cost_yuan": micros / 1_000_000,
        }

    def _persist(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._rows, ensure_ascii=False, indent=2), encoding="utf-8")
