"""协调当前规则产物：确认值或审查条件变化时，保留人工结论并重新核对。"""
from __future__ import annotations

import json
import hashlib
from decimal import Decimal

from .service import RulesAService
from .rules_b import RulesBService
from .common import load_tolerances


def _conditions_changed(issue, tolerances):
    rule = issue.get("rule_result") or {}
    field = {"尺寸": ("tolerance_mm", "dimension_tolerance_mm"),
             "标高": ("tolerance_m", "elevation_tolerance_m")}.get(rule.get("category"))
    if not field:
        return False
    acknowledged = rule.get("manual_recheck_acknowledged_tolerances") or {}
    previous = acknowledged.get(field[1], rule.get(field[0]))
    return previous is not None and Decimal(str(previous)) != Decimal(str(tolerances[field[1]]))



def _manual_context(issue, tolerances, facts, declarations):
    sides = []
    for side in ("left", "right"):
        matched = [{k: fact.get(k) for k in ("text", "numeric_value", "unit", "object_id", "comparison_key", "identity_confirmed", "confidence", "unclear")}
            for fact in facts if fact["unit_id"] == issue[f"{side}_unit_id"]
            and all(fact[k] == issue[f"{side}_{k}"] for k in ("x0", "y0", "x1", "y1"))]
        sides.append(sorted(matched, key=lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False)))
    key = {"尺寸":"dimension_tolerance_mm", "标高":"elevation_tolerance_m"}.get((issue.get("rule_result") or {}).get("category"))
    condition = {key: tolerances[key]} if key else {}
    category = (issue.get("rule_result") or {}).get("category")
    unit_context = []
    if key:
        for side in ("left", "right"):
            volume = declarations.volumes.get(issue[f"{side}_unit_id"])
            unit_context.append(sorted({(row["volume"], row["unit"], row["text"])
                for row in declarations.by_scope.get((volume, category), {}).values()}))
    payload = {"facts":sides, "conditions":condition}
    if any(unit_context):
        payload["unit_declarations"] = unit_context
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def acknowledge_manual_recheck(repo, issue_id):
    """Called only after an explicit human confirmed/excluded action, never by a model."""
    issue = repo.get("issues", issue_id)
    rr = dict(issue.get("rule_result") or {})
    if issue["review_status"] not in {"已确认", "排除"} or not rr.get("evidence_changed_after_confirmation"):
        return issue
    rr["manual_recheck_acknowledged_context"] = rr.get("manual_recheck_context")
    rr["manual_recheck_acknowledged_tolerances"] = rr.get("current_tolerance_snapshot") or load_tolerances(repo, None, None)
    rr.setdefault("manual_recheck_history", []).append({"context":rr.get("manual_recheck_context"),
        "tolerance_snapshot":rr["manual_recheck_acknowledged_tolerances"], "review_status":issue["review_status"], "review_note":issue.get("review_note", "")})
    rr.pop("evidence_changed_after_confirmation", None)
    rr.pop("review_conditions_changed_after_confirmation", None)
    repo._update("issues", issue_id, {"rule_result_json":json.dumps(rr, ensure_ascii=False)})
    # Historical report files and history snapshots remain immutable. Remove only
    # this resolved live coverage item, never a provider failure with the same issue id.
    for task in repo.list_records("review_tasks", version_id=issue["version_id"]):
        before = task.get("incomplete") or []
        after = [row for row in before if not (row.get("issue_id") == issue_id and row.get("step") == "rules_reconciliation")]
        if len(after) != len(before):
            repo.update_task(task["id"], status=task["status"], checkpoints=task["checkpoints"], incomplete=after,
                estimated_cost_micros=task["estimated_cost_micros"])
    return repo.get("issues", issue_id)

def reconcile_rule_results(repo, *, version_id, task_id=None, settings=None, trigger="confirmation"):
    repo._writable_version(version_id)
    issues = repo.list_records("issues", version_id=version_id)
    tolerances = load_tolerances(repo, task_id, settings)
    engines = {"rules_a", "rules_b"}
    removed = 0
    # Idempotent issue writers deliberately reuse identical evidence. Changed
    # tolerances require a fresh rule record/basis, even if that evidence still conflicts.
    with repo._transaction():
        for issue in issues:
            if ((issue.get("primary_opinion") or {}).get("engine") in engines
                    and issue["review_status"] == "疑似" and _conditions_changed(issue, tolerances)):
                repo._connection.execute("DELETE FROM issues WHERE id = ?", (issue["id"],))
                removed += 1
    a = RulesAService(repo).run(version_id, task_id=task_id, settings=settings)
    b = RulesBService(repo).run(version_id)
    active_ids = {issue["id"] for issue in a.issues + b.issues}
    from .rules_b import annotated_facts
    from .unit_declaration import volume_units
    units, effective_facts = annotated_facts(repo, version_id)
    declarations = volume_units(repo, version_id, units=units.values(), facts=effective_facts)
    manual = []
    with repo._transaction():
        for issue in issues:
            engine = (issue.get("primary_opinion") or {}).get("engine")
            if engine not in engines:
                continue
            changed = _conditions_changed(issue, tolerances)
            if issue["review_status"] == "疑似":
                if issue["id"] not in active_ids and not changed:
                    repo._connection.execute("DELETE FROM issues WHERE id = ?", (issue["id"],))
                    removed += 1
            elif issue["id"] not in active_ids or changed:
                context = _manual_context(issue, tolerances, effective_facts, declarations)
                if (issue.get("rule_result") or {}).get("manual_recheck_acknowledged_context") != context:
                    manual.append((issue, changed))
    for issue, conditions_changed in manual:
        rr = {**(issue.get("rule_result") or {}), "evidence_changed_after_confirmation": True}
        rr["current_tolerance_snapshot"] = tolerances
        rr["manual_recheck_context"] = _manual_context(issue, tolerances, effective_facts, declarations)
        if conditions_changed:
            rr["review_conditions_changed_after_confirmation"] = True
        note = issue.get("review_note") or ""
        reminder = "审查条件已修改，请重新核对" if conditions_changed else "标注已修改，请重新核对"
        if reminder not in note:
            note = (note + "；" if note else "") + reminder
        repo._update("issues", issue["id"], {"rule_result_json": json.dumps(rr, ensure_ascii=False), "review_note": note})
    unresolved_manual = [issue for issue in repo.list_records("issues", version_id=version_id)
        if issue["review_status"] != "疑似" and (issue.get("rule_result") or {}).get("evidence_changed_after_confirmation")]
    incomplete = list(a.incomplete) + list(b.incomplete) + [
        {"unit_id": issue["left_unit_id"], "issue_id": issue["id"], "category": "incomplete",
         "reason": "原人工结论的证据或审查条件已变化，需重新核对"}
        for issue in unresolved_manual]
    return {"removed_suspected": removed, "issue_count": len(a.issues) + len(b.issues),
        "manual_preserved": len(manual), "model_calls": 0, "incomplete": incomplete}


def recalculate_for_pending(repo, *, version_id, pending):
    return reconcile_rule_results(repo, version_id=version_id)
