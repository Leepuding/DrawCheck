import json
from types import SimpleNamespace

import pytest

from backend.evidence_selection import selected_facts
from backend.extract.semantic import SemanticError, scopes, validate
from backend.extract.semantic_job import VERSION, run_semantic_text
from backend.jobs import JobService, STEPS
from backend.jobs.plugins import NoOpPlugin, SemanticPlugin, StepRegistry
from backend.models.errors import ModelError
from backend.rules.semantic import run_semantic_comparisons
from backend.store import BBox, Evidence, Repository, Source
from tests.backend.jobs.conftest import configure_single_model

ROUTE = {"provider": "siliconflow", "model": "zai-org/GLM-4.5V"}


@pytest.fixture
def work(tmp_path):
    with Repository(tmp_path) as repo:
        project = repo.create_project("语义测试")
        version = repo.create_version(project["id"], "版本1")
        source = tmp_path / "synthetic.dwg"
        source.write_bytes(b"sample")
        original = repo.import_original(version["id"], source)["record"]
        yield repo, version["id"], original["id"]


def _unit(work, index, name, *, original=None):
    repo, version, source = work
    return repo.create_unit(version, original or source, kind="dwg_frame", ordinal=index, name=name,
        drawing_number=f"LAB-A101-{index:02d}", metadata={"extract_status": "ok"})


def _fact(work, unit, text, x, y, *, source=Source.CAD_TEXT, value=None, measure=None):
    repo, version, _ = work
    return repo.add_fact(version, Evidence(unit["id"], source, text, BBox(x-.01, y-.008, x+.01, y+.008)),
                         numeric_value=value, unit=measure)


def _case(work, *, count="7", unit="台", scope="本卷册", table_name="主要设备材料表", original=None):
    note = _unit(work, 1, "卷册说明")
    _fact(work, note, f"{scope}共配置250kVA备用变压器7台。", .15, .2)
    table = _unit(work, 2, table_name, original=original)
    for index, (header, value) in enumerate(zip(("序号", "名称", "规格", "单位", "数量"),
            ("1", "备用变压器", "250kVA", unit, count)), start=1):
        _fact(work, table, header, index*.12, .12)
        _fact(work, table, value, index*.12, .16)
    return note, table


def _wire(work, *, duplicate=False):
    overview = _unit(work, 1, "电气系统一次接线图")
    label = _fact(work, overview, "TA19", .5, .32)
    _fact(work, overview, "本期新建备用变压器TA19", .3, .28)
    detail = _unit(work, 2, "备用变压器一次接线图")
    _fact(work, detail, "本接线图适用备用变压器TA19", .25, .2)
    if duplicate:
        _fact(work, overview, "TA19", .65, .35)
    return overview, detail, label


def _quantity_records(source):
    records = []
    for row in source["rows"]:
        if row["role"] == "design_total":
            record = {"object": "备用变压器", "spec": "250kVA", "value": "7", "unit": "台"}
        elif row["role"] == "material_table":
            record = {key: row["cells"][key]["text"] for key in ("object", "spec", "value", "unit")}
        else:
            continue
        records.append({"row_id": row["row_id"], "kind": "quantity", **record})
    return {"records": records}


def _asset_records(source):
    return {"records": [{"kind": "asset", "row_id": row["row_id"], "tag": "TA19"}
                         for row in source["rows"] if row["role"] in {"overview", "equipment_scope"}]}


class TextModel:
    def __init__(self, answer, *, unknown_usage=False):
        self.answer = answer
        self.unknown_usage = unknown_usage
        self.requests = []

    def chat_completions(self, *, model, max_tokens, messages):
        assert model == ROUTE["model"] and max_tokens == 4096
        assert all(isinstance(message["content"], str) for message in messages)
        self.requests.append(json.loads(messages[1]["content"]))
        return SimpleNamespace(content=json.dumps(self.answer(self.requests[-1]), ensure_ascii=False),
                               input_tokens=None if self.unknown_usage else 100,
                               output_tokens=None if self.unknown_usage else 20)


def _run(work, model, *, route=ROUTE, gate=lambda: True, task_id=None):
    repo, version, _ = work
    if task_id is None:
        task_id = repo.create_task(version, checkpoints={"_meta": {"model_route": {"primary_model": route}}})["id"]
    return run_semantic_text(repo, version, task_id, route=route, client_factory=lambda: model, allow_model_call=gate)


def _compare(work, *, persist=False):
    repo, version, _ = work
    units = repo.list_records("drawing_units", version_id=version)
    return run_semantic_comparisons(repo, version, units, persist=persist)


@pytest.mark.parametrize(("count", "unit", "outcome"), [("7", "台", "match"), ("8", "台", "mismatch"), ("7", "套", "unverified")])
def test_quantity_requires_anchored_scope_and_uses_local_number_comparison(work, count, unit, outcome):
    note, table = _case(work, count=count, unit=unit)
    model = TextModel(_quantity_records)
    assert _run(work, model)["calls"] == 1
    result = _compare(work, persist=True)
    row, = result["comparisons"]
    assert (row["left_unit_id"], row["right_unit_id"], row["outcome"]) == (note["id"], table["id"], outcome)
    assert row["right_evidence"]["text"] == count
    assert len(row["semantic_evidence"]["right"]) == 5
    assert bool(result["issues"]) == (outcome == "mismatch")
    assert _run(work, model)["cache_hits"] == 1
    assert len(model.requests) == 1
    logged, = work[0].list_records("model_calls", version_id=work[1])
    assert logged["role"] == "primary" and logged["prompt_version"] == VERSION and logged["estimated_cost_micros"] > 0


@pytest.mark.parametrize("variant", ["single_building", "scoped_table", "other_source"])
def test_scope_and_source_are_not_inferred_from_quantity_or_title_similarity(work, variant):
    if variant == "other_source":
        repo, version, _ = work
        path = repo.root / "other.dwg"
        path.write_bytes(b"other")
        source = repo.import_original(version, path)["record"]
        _case(work, original=source["id"])
    else:
        _case(work, scope="A楼" if variant == "single_building" else "本卷册",
              table_name="A楼材料表" if variant == "scoped_table" else "主要设备材料表")
    _run(work, TextModel(_quantity_records))
    assert not _compare(work)["comparisons"]


def test_reject_invented_evidence_id_and_literal(work):
    _case(work)
    repo, version, _ = work
    units, facts = selected_facts(repo, version)
    (_, rows), = scopes(units, facts)
    for bad in ({"row_id": "r999"}, {"value": "70"}, {"unit": "套"}):
        record = {"kind": "quantity", "row_id": rows[0]["row_id"], "object": "备用变压器",
                  "spec": "250kVA", "value": "7", "unit": "台", **bad}
        with pytest.raises(SemanticError):
            validate({"records": [record]}, rows)


@pytest.mark.parametrize("variant", ["good", "duplicate", "conflict"])
def test_device_identity_requires_unique_literal_and_keeps_visual_conflict(work, variant):
    overview, _, _ = _wire(work, duplicate=variant == "duplicate")
    if variant != "duplicate":
        _fact(work, overview, "TA18" if variant == "conflict" else "TA19", .5, .32, source=Source.VISION)
    _run(work, TextModel(_asset_records))
    rows = _compare(work)["comparisons"]
    if variant == "duplicate":
        assert not rows
    else:
        row, = rows
        assert row["outcome"] == ("unverified" if variant == "conflict" else "match")
        assert bool(row["semantic_evidence"]["vision_conflicts"]) == (variant == "conflict")
        assert bool(row["semantic_evidence"]["vision_support"]) == (variant == "good")


def test_semantic_cost_gate_failure_and_route_change_leave_vision_intact(work):
    overview, _, _ = _wire(work)
    model = TextModel(_asset_records, unknown_usage=True)
    assert _run(work, model, gate=lambda: False)["cost_paused"]
    assert not model.requests
    assert _run(work, model)["cost"] > 0
    repo, version, _ = work
    assert repo.list_records("model_calls", version_id=version)[0]["status"] == "ok_usage_estimated"
    alternate = {**ROUTE, "base_url": "local-alternate-route"}

    def failed(_):
        raise ModelError("TIMEOUT")

    result = _run(work, TextModel(failed), route=alternate)
    assert result["incomplete"] and result["calls"] == 1
    assert repo.get("drawing_units", overview["id"])["metadata"]["extract_status"] == "ok"
    assert len(repo.list_records("model_calls", version_id=version)) == 2


def test_old_checkpoint_runs_semantics_only_after_disclosure(work, monkeypatch):
    from backend.settings import SettingsService
    from backend.settings.credentials import CredentialService, MemoryCredentialBackend

    _wire(work)
    repo, version, _ = work
    settings = SettingsService(repo, credentials=CredentialService(MemoryCredentialBackend(), allow_env=False))
    configure_single_model(settings)
    registry = StepRegistry()
    for step in STEPS:
        registry.register(NoOpPlugin(step))
    registry.register(SemanticPlugin())
    jobs = JobService(repo, settings_service=settings, registry=registry)
    old = repo.create_task(version, checkpoints={"_meta": {"model_route": jobs._model_route(repo.get_settings())},
        **{step: {"state": "done"} for step in STEPS if step not in {"semantic", "finalize"}}})
    jobs.get_progress(old["id"])
    assert not repo.list_records("model_calls", version_id=version)
    model = TextModel(_asset_records)
    monkeypatch.setattr(jobs, "_model_client_for_slot", lambda *_: model)
    disclosure = jobs.prepare_disclosure(version)["disclosure_id"]
    started = jobs.start_review(version, disclosure_id=disclosure)
    assert started["task_id"] == old["id"] and len(model.requests) == 1
    assert repo.get("review_tasks", old["id"])["checkpoints"]["semantic"]["state"] == "done"


@pytest.mark.parametrize("text", [
    "本卷册仅A楼配置250kVA备用变压器约7台，B楼另计。",
    "本卷册共不安装250kVA备用变压器7台。",
    "本卷册备用250kVA备用变压器约7台。",
])
def test_non_affirmative_or_approximate_scope_is_not_compared(work, text):
    note, _ = _case(work, scope="A楼")
    _fact(work, note, text, .15, .3)
    _run(work, TextModel(_quantity_records))
    assert not _compare(work)["comparisons"]


def test_capacity_suffix_and_ac_dc_subtypes_are_not_equivalent(work):
    from backend.extract.semantic import comparable_quantities

    note, _ = _case(work, scope="A楼")
    _fact(work, note, "本卷册共配置1250kVA备用变压器7台。", .15, .3)
    result = _run(work, TextModel(_quantity_records))
    assert result["incomplete"] and not _compare(work)["comparisons"]
    assert not comparable_quantities({"object": "交流汇流箱", "spec": "2进1出"},
                                     {"object": "直流汇流箱", "spec": "2进1出"})


def test_model_cannot_omit_conflicting_raw_total(work):
    note, _ = _case(work)
    _fact(work, note, "本卷册共配置250kVA备用变压器8台。", .16, .3)

    def omit_conflict(source):
        source["rows"] = [row for row in source["rows"]
                          if "8台" not in row["cells"].get("statement", {}).get("text", "")]
        return _quantity_records(source)

    assert _run(work, TextModel(omit_conflict))["incomplete"]
    assert not _compare(work)["comparisons"]


def test_missing_quantity_cell_cannot_be_filled_by_a_far_right_number(work):
    note = _unit(work, 1, "卷册说明")
    _fact(work, note, "本卷册共配置250kVA备用变压器7台。", .15, .2)
    table = _unit(work, 2, "主要设备材料表")
    for index, (header, value) in enumerate(zip(("序号", "名称", "规格", "单位", "数量"),
            ("1", "备用变压器", "250kVA", "台", "")), start=1):
        _fact(work, table, header, index*.1, .12)
        if value:
            _fact(work, table, value, index*.1, .16)
    _fact(work, table, "7", .94, .16)
    _run(work, TextModel(_quantity_records))
    assert not _compare(work)["comparisons"]


@pytest.mark.parametrize(("text", "y", "status", "outcome"), [
    ("备用变压器8台", .50, None, "match"),
    ("备用变压器7.0台", .20, None, "match"),
    ("备用变压器7台", .20, "已确认", "unverified"),
    ("备用变压器7台", .20, "无法确定", "unverified"),
])
def test_quantity_visual_evidence_uses_same_anchor_and_respects_human(work, text, y, status, outcome):
    note, _ = _case(work)
    repo, version, _ = work
    vision = _fact(work, note, text, .15, y, source=Source.VISION)
    meta = repo.get("drawing_units", note["id"])["metadata"]
    meta["fact_annotations"] = {vision["id"]: {"category": "数量与材料表"}}
    repo._update("drawing_units", note["id"], {"metadata_json": json.dumps(meta)})
    if status:
        pending = repo.create_pending(version, Evidence(note["id"], Source.VISION, text, BBox(.14, y-.008, .16, y+.008)), "读数不一致")
        # Same persisted bbox, including float roundoff.
        repo._update("pending_confirmations", pending["id"], {"status": status,
            "confirmed_text": "备用变压器8台" if status == "已确认" else None,
            **{key: vision[key] for key in ("x0", "y0", "x1", "y1")}})
    _run(work, TextModel(_quantity_records))
    row, = _compare(work)["comparisons"]
    assert row["outcome"] == outcome


def test_new_task_failure_does_not_reuse_stale_active_and_retry_can_recover(work):
    _wire(work)
    model = TextModel(_asset_records)
    _run(work, model)
    assert _compare(work)["comparisons"]
    repo, version, _ = work
    for cache in (repo.root / "cache" / "semantic").glob("*.json"):
        cache.unlink()
    task = repo.create_task(version, checkpoints={"_meta": {"model_route": {"primary_model": ROUTE}}})
    assert not _compare(work)["comparisons"]

    def failed(_):
        raise ModelError("TIMEOUT")

    assert _run(work, TextModel(failed), task_id=task["id"])["incomplete"]
    assert not _compare(work)["comparisons"]
    assert _run(work, model, task_id=task["id"])["calls"] == 1
    assert _compare(work)["comparisons"]


def test_asset_exclusion_and_reference_are_not_applicability():
    from backend.extract.semantic import asset_tags

    assert asset_tags("本接线图适用箱变1WBT06；不适用1WBT07，1WBT09详见另图。", "equipment_scope") == ["1WBT06"]


@pytest.mark.parametrize("kind", ["bare_note", "whole_table", "asset_unsure", "asset_edit"])
def test_manual_decision_cannot_leave_old_semantic_claim_verified(work, kind):
    repo, version, _ = work
    if kind.startswith("asset"):
        _, unit, _ = _wire(work)
        model = TextModel(_asset_records)
        text, x, y, measure, value = "TA19", .25, .2, None, None
        category = "编号写法"
    else:
        note, table = _case(work)
        model = TextModel(_quantity_records)
        if kind == "bare_note":
            unit, text, x, y, measure, value = note, "备用变压器7台", .15, .2, "台", "7"
        else:
            unit, text, x, y, measure, value = table, "1 备用变压器 250kVA 台 7", .6, .16, "台", "7"
        category = "数量与材料表"
    vision = _fact(work, unit, text, x, y, source=Source.VISION, value=value, measure=measure)
    meta = repo.get("drawing_units", unit["id"])["metadata"]
    meta["fact_annotations"] = {vision["id"]: {"category": category}}
    repo._update("drawing_units", unit["id"], {"metadata_json": json.dumps(meta)})
    bbox = BBox(*(vision[key] for key in ("x0", "y0", "x1", "y1")))
    pending = repo.create_pending(version, Evidence(unit["id"], Source.VISION, text, bbox), "读数不一致")
    correction = {"bare_note": "8", "whole_table": "1 备用变压器 250kVA 台 8", "asset_edit": "TA18"}.get(kind)
    repo._update("pending_confirmations", pending["id"], {
        "status": "已确认" if correction else "无法确定", "confirmed_text": correction})
    assert _run(work, model)["calls"] == 1
    row, = _compare(work)["comparisons"]
    assert row["outcome"] == "unverified"


def test_explicit_restart_recovers_failed_semantics_and_runs_downstream(work, monkeypatch):
    from backend.settings import SettingsService
    from backend.settings.credentials import CredentialService, MemoryCredentialBackend

    _wire(work)
    repo, version, _ = work
    settings = SettingsService(repo, credentials=CredentialService(MemoryCredentialBackend(), allow_env=False))
    configure_single_model(settings)
    registry = StepRegistry()
    for step in STEPS:
        registry.register(NoOpPlugin(step))
    registry.register(SemanticPlugin())
    checked = []

    class Check(NoOpPlugin):
        def run(self, ctx):
            checked.append(_compare(work)["comparisons"])
            return super().run(ctx)

    registry.register(Check("rules_a"))
    jobs = JobService(repo, settings_service=settings, registry=registry)
    attempts = []

    def answer(source):
        attempts.append(source)
        if len(attempts) == 1:
            raise ModelError("TIMEOUT")
        return _asset_records(source)

    model = TextModel(answer)
    monkeypatch.setattr(jobs, "_model_client_for_slot", lambda *_: model)
    first = jobs.start_review(version, disclosure_id=jobs.prepare_disclosure(version)["disclosure_id"])
    assert first["outcome"] == "partial" and checked == [[]]
    second = jobs.start_review(version, disclosure_id=jobs.prepare_disclosure(version)["disclosure_id"])
    assert second["task_id"] != first["task_id"] and len(attempts) == 2
    assert checked[1][0]["outcome"] == "match"


@pytest.mark.parametrize("separator", ["。", "，"])
def test_volume_scope_does_not_leak_to_building_clause(work, separator):
    note, _ = _case(work, scope="A楼")
    _fact(work, note, f"本卷册共配置消防泵2台{separator}B楼拟安装250kVA备用变压器7台。", .15, .3)
    assert _run(work, TextModel(_quantity_records))["incomplete"]
    assert not _compare(work)["comparisons"]


def test_valid_one_side_response_records_coverage_and_is_retryable(work):
    _wire(work)

    def one_side(source):
        return _asset_records({"rows": [row for row in source["rows"] if row["role"] == "overview"]})

    model = TextModel(one_side)
    result = _run(work, model)
    assert result["incomplete"] and not _compare(work)["comparisons"]
    coverage = result["diagnostics"][0]["coverage"]
    assert not coverage["has_pair_candidates"]
    assert {row["role"]: row["outcome"] for row in coverage["rows"]} == {"overview": "covered", "equipment_scope": "omitted"}
    assert _run(work, TextModel(_asset_records))["calls"] == 1
    assert _compare(work)["comparisons"][0]["outcome"] == "match"


def _component_and_combiner(work):
    note = _unit(work, 1, "卷册说明")
    _fact(work, note, "本卷册共采用300Wp组件12块，汇流箱1台。", .15, .2)
    table = _unit(work, 2, "主要设备材料表")
    values = [("序号", "名称", "规格", "单位", "数量"), ("1", "光伏组件", "300Wp", "块", "12"),
              ("2", "交流汇流箱", "2进1出", "台", "1")]
    for line, cells in enumerate(values):
        for column, text in enumerate(cells, 1):
            _fact(work, table, text, column*.12, .12+line*.04)


def _component_reply(source, *, component="光伏组件", invented=False, conflicting=False):
    records = []
    for row in source["rows"]:
        base = {"kind": "quantity", "row_id": row["row_id"]}
        if row["role"] == "design_total":
            records.append({**base, "object": component, "spec": "300Wp", "value": "12", "unit": "块"})
            if not invented or conflicting:
                records.append({**base, "object": "汇流箱", "spec": "", "value": "1", "unit": "台"})
            if invented:
                records.append({**base, "object": "交流汇流箱", "spec": "2进1出", "value": "1", "unit": "台"})
        else:
            records.append({**base, **{key: row["cells"][key]["text"] for key in ("object", "spec", "value", "unit")}})
    return {"records": records}


@pytest.mark.parametrize(("name", "accepted"), [("组件", True), ("光伏组件", True), ("储能组件", False)])
def test_note_accepts_only_declared_aliases_of_its_anchored_short_name(work, name, accepted):
    from backend.extract.semantic import evidence_payload

    _component_and_combiner(work)
    repo, version, _ = work
    units, facts = selected_facts(repo, version)
    (_, rows), = scopes(units, facts)
    payload = _component_reply({"rows": evidence_payload(rows)}, component=name)
    if accepted:
        records = validate(payload, rows)
        assert next(record for record in records if record["role"] == "design_total")["snippet"].endswith("300Wp组件12块")
    else:
        with pytest.raises(SemanticError, match="quantity note anchors"):
            validate(payload, rows)


@pytest.mark.parametrize("conflicting", [False, True])
def test_cross_row_spec_invention_rejected_while_independent_generic_count_survives(work, conflicting):
    from backend.extract.semantic import evidence_payload

    _component_and_combiner(work)
    repo, version, _ = work
    units, facts = selected_facts(repo, version)
    (_, rows), = scopes(units, facts)
    answer = lambda source: _component_reply(source, invented=True, conflicting=conflicting)
    with pytest.raises(SemanticError, match="quantity note anchors"):
        validate(answer({"rows": evidence_payload(rows)}), rows)
    model = TextModel(answer)
    first = _run(work, model)
    rejected = first["diagnostics"][0]["record_rejections"]
    assert len(rejected) == 1 and "quantity note anchors" in rejected[0]
    coverage = first["diagnostics"][0]["model_coverage"]["rows"]
    note, = [row for row in coverage if row["role"] == "design_total"]
    assert note["expected"] == 2 and note["returned"] == 1 and note["outcome"] == "unresolved"
    assert not first["incomplete"]
    local_note, = [row for row in first["diagnostics"][0]["coverage"]["rows"] if row["role"]=="design_total"]
    assert local_note["returned"]==2 and local_note["outcome"]=="covered"
    compared = _compare(work)["comparisons"]
    assert [(row["label"], row["outcome"]) for row in compared] == [("光伏组件 300WP", "match"), ("汇流箱", "match")]
    generic = compared[-1]
    assert generic["semantic_evidence"]["left_reader"] == "local_anchored_text"
    assert "交流" not in generic["label"] and "2进1出" not in generic["label"]
    reused=_run(work,model,gate=lambda:False)
    assert reused["cache_hits"]==0 and reused["active_reuse_hits"]==1 and reused["calls"]==0
    assert len(model.requests) == 1
    assert {record["status"] for record in repo.list_records("model_calls", version_id=version)} == {"partial"}


def test_local_totals_work_without_model_and_preserve_generic_identity(work):
    from backend.extract.semantic import local_quantity_records
    _component_and_combiner(work)
    repo, version, _ = work
    units, facts = selected_facts(repo, version)
    (_, rows), = scopes(units, facts)
    counts = local_quantity_records(rows)
    generic, = [item for item in counts if item["role"] == "design_total" and item["object"] == "汇流箱"]
    assert generic["spec"] == "" and generic["value"] == "1"
    assert len(_compare(work)["comparisons"]) == 2
    assert not repo.list_records("model_calls", version_id=version)


@pytest.mark.parametrize("variant", ["good", "other_name", "conditional", "conflicting", "human_conflict"])
def test_material_spec_requires_complete_model_same_use_and_respects_manual_conflict(work, variant):
    note, table = _case(work)
    # Replace the table object/spec while retaining independently positioned cells.
    repo, version, _ = work
    for fact in repo.list_records("facts", version_id=version):
        if fact["unit_id"] != table["id"]:
            continue
        if fact["text"] == "备用变压器":
            repo._update("facts", fact["id"], {"text": "光伏专用电缆"})
        elif fact["text"] == "250kVA":
            repo._update("facts", fact["id"], {"text": "H1Z2Z2-1X4mm²"})
    statement = "光伏组件间以H1Z2Z2-1X4mm2光伏专用电缆连接"
    if variant == "other_name":
        statement = statement.replace("光伏专用电缆", "直流电缆")
    if variant == "conditional":
        statement = "例如" + statement
    _fact(work, note, statement, .3, .3)
    if variant == "conflicting":
        _fact(work, note, statement.replace("1X4", "1X6"), .3, .4)
    if variant == "human_conflict":
        visual = _fact(work, note, statement, .3, .3, source=Source.VISION)
        repo._update("facts", visual["id"], {"source": Source.USER.value, "text": statement.replace("1X4", "1X6")})
    result = [row for row in _compare(work)["comparisons"] if row["category"] == "材料规格"]
    if variant in {"other_name", "conditional", "conflicting"}:
        assert result == []
    elif variant == "human_conflict":
        # Two distinct local claims cannot silently select the matching one.
        assert result == []
    else:
        row, = result
        assert row["outcome"] == "match" and row["left_value"] == row["right_value"] == "H1Z2Z2-1X4MM2"


@pytest.mark.parametrize("variant", ["saved_visual", "missing", "visual_conflict", "duplicate"])
def test_truncated_native_cable_area_never_invented_and_saved_visual_is_independent(work, variant):
    note, table = _case(work)
    repo, version, _ = work
    for fact in repo.list_records("facts", version_id=version):
        if fact["unit_id"] != table["id"]:
            continue
        if fact["text"] == "备用变压器":
            repo._update("facts", fact["id"], {"text": "光伏专用电缆"})
        elif fact["text"] == "250kVA":
            repo._update("facts", fact["id"], {"text": "H1Z2Z2-1X4mm"})
    _fact(work, note, "光伏组件间以H1Z2Z2-1X4mm2光伏专用电缆连接", .3, .3)
    if variant != "missing":
        _fact(work, table, "1 光伏专用电缆 H1Z2Z2-1X4mm² 台 7", .36, .16, source=Source.VISION)
    if variant == "duplicate":
        _fact(work, note, "光伏组件间以H1Z2Z2-1X4mm2光伏专用电缆连接", .3, .4)
    if variant == "visual_conflict":
        _fact(work, table, "1 光伏专用电缆 H1Z2Z2-1X6mm² 台 7", .36, .16, source=Source.VISION)
    result = [row for row in _compare(work)["comparisons"] if row["category"] == "材料规格"]
    if variant in {"missing", "visual_conflict"}:
        assert result == []
    else:
        row, = result
        assert row["outcome"] == "match"
        assert row["semantic_evidence"]["right_reader"] == "saved_visual_anchored_table"
        assert any(item["text"] == "H1Z2Z2-1X4mm" for item in row["semantic_evidence"]["right"])
        assert any("mm²" in item["text"] for item in row["semantic_evidence"]["right"])


@pytest.mark.parametrize("area, expected_difference", [("4", 0), ("6", 1)])
def test_semantic_spec_uses_standard_material_category_in_normal_result_api(work, area, expected_difference):
    from PIL import Image
    from backend.api.review_status import list_issues
    from backend.rules import RulesAService

    note, table = _case(work)
    repo, version, _ = work
    for unit in (note, table):
        preview = f"{unit['id']}.png"
        Image.new("RGB", (10, 10)).save(repo.root / preview)
        repo._update("drawing_units", unit["id"], {"metadata_json": json.dumps(
            {**unit["metadata"], "review_relative_path": preview})})
    for fact in repo.list_records("facts", version_id=version):
        if fact["unit_id"] == table["id"]:
            if fact["text"] == "备用变压器":
                repo._update("facts", fact["id"], {"text": "光伏专用电缆"})
            elif fact["text"] == "250kVA":
                repo._update("facts", fact["id"], {"text": f"H1Z2Z2-1X{area}mm²"})
    _fact(work, note, "光伏组件间以H1Z2Z2-1X4mm2光伏专用电缆连接", .3, .3)

    RulesAService(repo).run(version)
    bundle = list_issues(repo, version)
    coverage = {row["category"]: row for row in bundle["comparison_summary"]}
    assert coverage["材料规格"] == {"category": "材料规格", "difference": expected_difference,
                                    "unverified": 0, "verified": True}
    assert "规格" not in coverage
    findings = [row for row in bundle["items"] if row["category"] == "材料规格"]
    assert len(findings) == expected_difference
    assert not bundle["comparison_details"]
    assert not repo.list_records("model_calls", version_id=version)


@pytest.mark.parametrize("change",["source","route","invalidated","version"])
def test_partial_active_reuse_revalidates_source_route_version_and_manual_invalidation(work,change):
    _component_and_combiner(work)
    model=TextModel(lambda source:_component_reply(source,invented=True))
    assert not _run(work,model)["incomplete"]
    repo,version,_=work
    holder=next(unit for unit in repo.list_records("drawing_units",version_id=version) if unit["metadata"].get("semantic_text"))
    state=holder["metadata"]["semantic_text"]
    if change=="source":
        fact=next(fact for fact in repo.list_records("facts",version_id=version) if "组件12块" in fact["text"])
        repo._update("facts",fact["id"],{"text":fact["text"].replace("组件12块","组件13块")})
    elif change in {"invalidated","version"}:
        state[change]=True if change=="invalidated" else "other-reader"
        repo._update("drawing_units",holder["id"],{"metadata_json":json.dumps(holder["metadata"],ensure_ascii=False)})
    route={**ROUTE,"base_url":"https://different.invalid"} if change=="route" else ROUTE
    result=_run(work,model,route=route,gate=lambda:False)
    assert result["calls"]==0 and result["cost_paused"]
    assert result["active_reuse_hits"]==0 and len(model.requests)==1


def test_structured_visual_serial_can_anchor_unprefixed_material_row_without_borrowing():
    from backend.extract.engineering import table_cable_specification
    text="光伏专用电缆 H1Z2Z2-1X4mm² 米 1500 以实际为准"
    assert table_cable_specification(text,"2","光伏专用电缆",row_identity="序号2光伏专用电缆")=="H1Z2Z2-1X4MM2"
    for identity in (None,"序号3光伏专用电缆","序号2直流电缆","光伏专用电缆"):
        assert table_cable_specification(text,"2","光伏专用电缆",row_identity=identity) is None


@pytest.mark.parametrize("case",["positioned","clamped","whole_page","sliver_overlap"])
def test_structured_serial_cannot_replace_a_reliable_physical_material_row(work,case):
    note,table=_case(work);repo,version,_=work
    for fact in repo.list_records("facts",version_id=version):
        if fact["unit_id"]==table["id"]:
            if fact["text"]=="备用变压器":repo._update("facts",fact["id"],{"text":"光伏专用电缆"})
            elif fact["text"]=="250kVA":repo._update("facts",fact["id"],{"text":"H1Z2Z2-1X4mm"})
    _fact(work,note,"光伏组件间以H1Z2Z2-1X4mm2光伏专用电缆连接",.3,.3)
    box=BBox(.3,.152,.7,.168)
    if case=="whole_page":box=BBox(0,0,1,1)
    elif case=="sliver_overlap":box=BBox(.3,.167,.7,.20)
    visual=repo.add_fact(version,Evidence(table["id"],Source.VISION,"光伏专用电缆 H1Z2Z2-1X4mm² 米1500",box))
    unit=repo.get("drawing_units",table["id"])
    unit["metadata"]["fact_annotations"]={visual["id"]:{"object_id":"序号1光伏专用电缆","quantity_role":"material_table", "confidence":.9,"unclear":False,"bbox_clamped":case=="clamped"}}
    repo._update("drawing_units",unit["id"],{"metadata_json":json.dumps(unit["metadata"],ensure_ascii=False)})
    rows=[row for row in _compare(work)["comparisons"] if row["category"]=="材料规格"]
    assert bool(rows)==(case=="positioned")
