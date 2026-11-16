"""Run and meter an independent text-only model stage."""
from collections import Counter
import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from backend.evidence_selection import selected_facts
from backend.extract.errors import ExtractError
from backend.extract.schema import parse_model_json
from backend.models.cost import estimate_cost_micros
from backend.models.errors import ModelError
from backend.models.presets import price_for_model
from backend.store import StoreError

from .semantic import MAX_INPUT_BYTES, MAX_ROWS, MAX_TOKENS, PROMPT, VERSION, SemanticError, adopt_response, coverage, evidence_payload, local_quantity_records, scopes, validate


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def source_input(family, rows):
    return _json({"drawing_family": family, "rows": evidence_payload(rows)})


def _cache_path(root, source, route):
    return Path(root) / "cache" / "semantic" / (_sha(_json([VERSION, _sha(source), route])) + ".json")


def _cached(path, rows):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        validate(payload, rows)
        return payload
    except (OSError, ValueError):
        return None


def _save_cache(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            tmp_path = Path(handle.name)
            handle.write(_json(payload))
        tmp_path.replace(path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _save_active(store, unit_id, family, source, payload, route, task_id, checked, audit=None):
    with store._transaction():
        unit = store.get("drawing_units", unit_id)
        metadata = unit["metadata"]
        metadata["semantic_text"] = {"version": VERSION, "task_id": task_id, "family": family, "source_sha256": _sha(source),
                                     "route_sha256": _sha(_json(route)), "payload": payload, "coverage": checked,
                                     "audit": audit or {}}
        store._update("drawing_units", unit_id, {"metadata_json": _json(metadata)})


def _active_records(unit, family, rows, route, task_id):
    state = (unit.get("metadata") or {}).get("semantic_text") or {}
    if (state.get("invalidated") or state.get("version") != VERSION or state.get("task_id") != task_id or state.get("family") != family
            or state.get("source_sha256") != _sha(source_input(family, rows))
            or state.get("route_sha256") != _sha(_json(route))):
        return []
    try:
        return validate(state.get("payload"), rows)
    except SemanticError:
        return []


def _invalidate_active(store, unit_id, task_id):
    with store._transaction():
        metadata = store.get("drawing_units", unit_id)["metadata"]
        state = metadata.get("semantic_text") or {}
        if state.get("task_id") == task_id:
            state["invalidated"] = True
            store._update("drawing_units", unit_id, {"metadata_json": _json(metadata)})


def _reusable_active(store, unit, family, rows, route):
    """Revalidate an adopted partial reply without promoting it to full cache.

    Source/route/version and the originating metered call must all match. The
    model's unresolved rows and rejection audit remain unchanged; local exact
    readings may independently resolve a business quantity on the same row.
    """
    state=(unit.get("metadata") or {}).get("semantic_text") or {}
    origin=(state.get("audit") or {}).get("origin_task_id") or state.get("task_id")
    if not origin or not _active_records(unit,family,rows,route,state.get("task_id")):
        return None
    try:
        task=store.get("review_tasks",origin)
    except (StoreError,KeyError,ValueError):
        return None
    if task.get("version_id")!=unit["version_id"]:
        return None
    calls=store.list_records("model_calls",version_id=unit["version_id"])
    call=next((call for call in calls if call["task_id"]==origin and call.get("role")=="primary" and call.get("prompt_version")==VERSION and
               call.get("image_sha256")==state["source_sha256"] and call.get("provider")==route["provider"] and
               call.get("model")==route["model"] and call.get("status") in {"ok","partial","ok_usage_estimated","partial_usage_estimated"}),None)
    if call is None:
        return None
    payload=state["payload"]
    records=validate(payload,rows)
    fused,detail=_quantity_fused_coverage(rows,records,payload.get("unresolved_row_ids",[]))
    original=detail["model_coverage"]
    partial=bool((state.get("audit") or {}).get("record_rejections")) or any(row["outcome"]!="covered" for row in original["rows"])
    if not partial or any(row["outcome"]!="covered" for row in fused["rows"]) or not fused["has_pair_candidates"]:
        return None
    return payload,{**(state.get("audit") or {}),"origin_task_id":origin,"origin_model_call_id":call["id"],
                    "adopted_payload_sha256":_sha(_json(payload)),"active_partial_revalidated":True}


def _quantity_fused_coverage(rows, records, unresolved):
    model=coverage(rows,records,unresolved)
    local=local_quantity_records(rows)
    claims={}
    for record in local:
        if record["role"]=="design_total":
            subject=record["object"],record["spec"],record["unit"]
            claims.setdefault(subject,set()).add(record["value"])
    ambiguous={record["row_id"] for record in local if record["role"]=="design_total" and
               len(claims[record["object"],record["spec"],record["unit"]])>1}
    local=[record for record in local if record["row_id"] not in ambiguous]
    local_checked=coverage(rows,local,[])
    complete={row["row_id"] for row in local_checked["rows"] if row["outcome"]=="covered" and row["role"] in {"design_total","material_table"}}
    merged=list(records)
    signatures={_json({key:record.get(key) for key in ("row_id","kind","object","spec","value","unit")}) for record in records}
    for record in local:
        key=_json({field:record.get(field) for field in ("row_id","kind","object","spec","value","unit")})
        if key not in signatures:
            merged.append(record);signatures.add(key)
    checked=coverage(rows,merged,[row_id for row_id in unresolved if row_id not in complete])
    return checked,{"model_coverage":model,"local_quantity_coverage":local_checked,
                    "locally_completed_row_ids":sorted(complete),"local_quantity_reader":"local-anchored-quantity-v1"}


def active_records(store, version_id, units, *, task_id=None):
    if not hasattr(store, "list_records"):
        return []
    task = None
    if task_id:
        task = store.get("review_tasks", task_id)
    else:
        tasks = store.list_records("review_tasks", version_id=version_id)
        task = tasks[-1] if tasks else None
    route = (((task or {}).get("checkpoints", {}).get("_meta") or {}).get("model_route") or {}).get("primary_model")
    by_id, facts = selected_facts(store, version_id)
    groups = []
    for scope, rows in scopes(by_id, facts):
        records = local_quantity_records(rows)
        model = (_active_records(by_id[rows[0]["unit_id"]], scope[1], rows, route, task["id"])
                 if route and task.get("version_id") == version_id else [])
        # The local parser and model can independently read the same item. Keep
        # one claim per anchored literal so they never create duplicate pairs.
        seen = {_json({key: item.get(key) for key in ("row_id", "kind", "object", "spec", "value", "unit", "tag", "target")})
                for item in records}
        for item in model:
            key = _json({key: item.get(key) for key in ("row_id", "kind", "object", "spec", "value", "unit", "tag", "target")})
            if key not in seen:
                records.append(item)
                seen.add(key)
        groups.append((scope, rows, records))
    return groups


def run_semantic_text(store, version_id, task_id, *, route, client_factory, allow_model_call,
                      report=None, cancelled=None, record_diagnostic=None):
    """A failure leaves visual data and the previous audit record intact."""
    units, facts = selected_facts(store, version_id)
    work = list(scopes(units, facts))
    calls = cost = cache_hits = active_reuse_hits = 0
    incomplete = []
    diagnostics = []
    cost_paused = False
    client = None
    if report:
        report(0, len(work), unit="组图纸")
    for index, ((_, family), rows) in enumerate(work):
        unit_id = rows[0]["unit_id"]
        if cancelled is not None and cancelled.is_set():
            incomplete.append({"unit_id": unit_id, "category": "incomplete", "reason": "文字语义分析已中断"})
            return {"calls": calls, "cost": cost, "cache_hits": cache_hits, "incomplete": incomplete, "interrupted": True}
        source = source_input(family, rows)
        if len(rows) > MAX_ROWS or len(source.encode()) > MAX_INPUT_BYTES:
            incomplete.append({"unit_id": unit_id, "category": "incomplete", "reason": "文字语义输入超出本次安全范围"})
            continue
        path = _cache_path(store.root, source, route)
        payload = _cached(path, rows)
        detail = {"sheet": rows[0]["sheet"], "input_rows": len(rows), "cached": payload is not None}
        audit={}
        if payload is not None:
            state=(units[unit_id].get("metadata") or {}).get("semantic_text") or {}
            if (_active_records(units[unit_id],family,rows,route,state.get("task_id")) and state.get("payload")==payload):
                audit={**(state.get("audit") or {}),"origin_task_id":(state.get("audit") or {}).get("origin_task_id") or state.get("task_id")}
            audit["cached_adopted_payload_sha256"]=_sha(_json(payload))
        if payload is None:
            reused=_reusable_active(store,units[unit_id],family,rows,route)
            if reused:
                payload,audit=reused
                detail["active_reused"]=True
                detail["origin_model_call_id"]=audit["origin_model_call_id"]
        if payload is None:
            _invalidate_active(store, unit_id, task_id)
            if allow_model_call is None or not allow_model_call():
                cost_paused = True
                incomplete.append({"unit_id": unit_id, "category": "incomplete", "reason": "费用上限，审查已暂停"})
                break
            result = None
            status = "error"
            input_tokens = output_tokens = charge = 0
            calls += 1
            try:
                if client is None:
                    client = client_factory()
                result = client.chat_completions(model=route["model"], max_tokens=MAX_TOKENS, messages=[
                    {"role": "system", "content": PROMPT}, {"role": "user", "content": source},
                ])
                input_tokens, output_tokens = result.input_tokens, result.output_tokens
                price = price_for_model(route["provider"], route["model"])
                usage_unknown = input_tokens is None or output_tokens is None
                charge = estimate_cost_micros(
                    input_tokens=len((PROMPT + source).encode()) + 256 if input_tokens is None else input_tokens,
                    output_tokens=MAX_TOKENS if output_tokens is None else output_tokens,
                    input_price_per_m_yuan=price.input_per_m_yuan, output_price_per_m_yuan=price.output_per_m_yuan,
                ).estimated_cost_micros or 0
                if getattr(result, "finish_reason", None) == "length":
                    raise SemanticError("truncated response")
                payload = parse_model_json(result.content)
                audit={"origin_task_id":task_id,"visible_response_sha256":_sha(result.content)}
                payload, records, rejected = adopt_response(payload, rows)
                status = "partial" if rejected else "ok"
                if usage_unknown:
                    status += "_usage_estimated"
                if rejected:
                    detail["record_rejections"] = rejected
                    audit["record_rejections"]=rejected
            except ModelError as exc:
                detail["model_error"] = exc.code
                incomplete.append({"unit_id": unit_id, "category": "incomplete",
                                   "reason": f"文字语义模型调用失败（{exc.code}）"})
            except (ExtractError, SemanticError) as exc:
                status = "bad_format"
                detail["validation_error"] = str(exc) if isinstance(exc, SemanticError) else "invalid response JSON"
                incomplete.append({"unit_id": unit_id, "category": "incomplete",
                                   "reason": "文字语义输出未能核实原文或结构"})
            store.record_model_call(task_id, unit_id, role="primary", provider=route["provider"], model=route["model"],
                status=status, input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0),
                estimated_cost_micros=charge, image_sha256=_sha(source), prompt_version=VERSION)
            cost += charge
            if status not in {"ok", "ok_usage_estimated", "partial", "partial_usage_estimated"}:
                diagnostics.append(detail)
                if record_diagnostic:
                    record_diagnostic(detail)
                continue
        else:
            if detail.get("active_reused"):
                active_reuse_hits += 1
            else:
                cache_hits += 1
            records = validate(payload, rows)
        checked,fusion = _quantity_fused_coverage(rows,records,payload.get("unresolved_row_ids",[]))
        detail.update(fusion)
        detail["coverage"] = checked
        diagnostics.append(detail)
        if record_diagnostic:
            record_diagnostic(detail)
        missing = [row for row in checked["rows"] if row["outcome"] != "covered"]
        if not detail["cached"] and not detail.get("active_reused") and not detail.get("record_rejections") and not any(row["outcome"] == "omitted" for row in missing):
            _save_cache(path, payload)
        _save_active(store, unit_id, family, source, payload, route, task_id, checked,{**audit,**fusion})
        if missing:
            units_by_row = {row["row_id"]: row["unit_id"] for row in rows}
            for missing_unit, count in Counter(units_by_row[row["row_id"]] for row in missing).items():
                incomplete.append({"unit_id": missing_unit, "category": "incomplete",
                                   "reason": f"有 {count} 条文字语义候选未完整核实"})
        elif not checked["has_pair_candidates"]:
            incomplete.append({"unit_id": unit_id, "category": "incomplete",
                               "reason": "未形成两侧可对照的文字语义记录"})
        if report:
            report(index + 1, len(work), unit="组图纸")
    return {"calls": calls, "cost": cost, "cache_hits": cache_hits, "incomplete": incomplete,
            "active_reuse_hits":active_reuse_hits,"cost_paused": cost_paused, "diagnostics": diagnostics}
