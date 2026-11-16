"""规则 A 编排服务。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.store import BBox, Evidence, Repository, Source

from .catalog_titleblock import run_catalog_titleblock
from .dimension import run_dimension
from .elevation import run_elevation
from .engineering import run_engineering
from .common import write_pending
from .unit_declaration import volume_units


@dataclass
class RulesAResult:
    issues: list[dict[str, Any]] = field(default_factory=list)
    pendings: list[dict[str, Any]] = field(default_factory=list)
    incomplete: list[dict[str, Any]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


class RulesAService:
    def __init__(self, store: Repository):
        self.store = store

    def run(
        self,
        version_id: str,
        *,
        task_id: str | None = None,
        settings: dict[str, Any] | None = None,
        expected_prefix: str | None = None,
        persist: bool = True,
    ) -> RulesAResult:
        units = self.store.list_records("drawing_units", version_id=version_id)
        incomplete: list[dict[str, Any]] = []
        for u in units:
            meta = u.get("metadata") or {}
            if meta.get("extract_status") == "incomplete":
                incomplete.append({
                    "unit_id": u["id"],
                    "reason": meta.get("extract_incomplete_reason") or "识别未完成，规则部分范围 incomplete",
                    "category": "incomplete",
                })

        cat = run_catalog_titleblock(
            self.store,
            version_id,
            expected_prefix=expected_prefix,
            persist=persist,
        )
        declarations = volume_units(self.store, version_id, units=units, refresh=persist)
        dim = run_dimension(
            self.store,
            version_id,
            task_id=task_id,
            settings=settings,
            persist=persist,
            declarations=declarations,
        )
        elv = run_elevation(
            self.store,
            version_id,
            task_id=task_id,
            settings=settings,
            persist=persist,
            declarations=declarations,
        )

        engineering = run_engineering(self.store, version_id, task_id=task_id, settings=settings,
                                      persist=persist, units=units, declarations=declarations)

        issues = list(cat.get("issues") or []) + list(dim.get("issues") or []) + list(elv.get("issues") or []) + engineering["issues"]
        pendings = []
        for reason, evidence in declarations.conflicts():
            first = evidence[0]
            if persist:
                pendings.append(write_pending(self.store, version_id, Evidence(
                    first["unit_id"], Source(first["source"]), first["text"], BBox(**first["bbox"])), reason))
            else:
                pendings.append({"unit_id": first["unit_id"], "text": first["text"], "reason": reason})
        incomplete.extend(cat.get("incomplete") or [])
        incomplete.extend(dim["incomplete"])
        incomplete.extend(elv["incomplete"])

        return RulesAResult(
            issues=issues,
            pendings=pendings,
            incomplete=incomplete,
            detail={
                "catalog": {"issue_count": len(cat.get("issues") or []),
                            "local_comparison_count": len(cat.get("local_comparisons") or [])},
                "engineering": {"comparison_count": sum(row["outcome"] in {"match", "mismatch"}
                                                        for row in engineering["comparisons"]),
                                "unverified_count": sum(row["outcome"] in {"unverified", "linked"}
                                                        for row in engineering["comparisons"]),
                                "issue_count": len(engineering["issues"])},
                "dimension": {
                    "issue_count": len(dim.get("issues") or []),
                    "pending_count": len(dim.get("pendings") or []),
                    "tolerance_mm": dim.get("tolerance_mm"),
                },
                "elevation": {
                    "issue_count": len(elv.get("issues") or []),
                    "tolerance_m": elv.get("tolerance_m"),
                },
            },
        )
