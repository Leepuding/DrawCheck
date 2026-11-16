"""Behavioral integration acceptance; failures identify unmet contracts, never xfail.

The scripted model replaces the external inference boundary. All application,
extraction, rule, persistence, revision, review and report code remains real.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

from backend.export import ExportService
from backend.importer import ImportService
from backend.jobs import JobService
from backend.models.errors import ModelError, CODE_TIMEOUT
from backend.models.presets import same_base_model
from backend.models.cost import estimate_cost_micros
from backend.models.presets import price_for_model
from backend.revisions import RevisionService
from backend.review import ReviewService
from backend.settings.service import SettingsService, SettingsError
from backend.store import StoreError

from .conftest import ScriptedModel, fact, payload, make_issue, finish_task


def run_script(repo, drawing, monkeypatch, responses):
    scripted = ScriptedModel(list(responses))
    monkeypatch.setattr(SettingsService, "_make_client", lambda *args: scripted)
    result = JobService(repo).start_review(drawing["vid"])
    return result, scripted


def test_unconfigured_start_creates_no_task(client, repo, drawing):
    response = client.post(f"/api/versions/{drawing['vid']}/tasks")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NO_MODEL"
    assert repo.list_records("review_tasks", version_id=drawing["vid"]) == []


@pytest.mark.parametrize("method,path", [
    ("get", "/api/health"), ("get", "/api/settings"),
    ("post", "/api/import"), ("get", "/api/issues?version_id=missing"),
    ("get", "/api/exports/missing/download"), ("get", "/api/history"),
])
def test_every_api_family_requires_local_token(client, method, path):
    client.headers.pop("Authorization")
    response = getattr(client, method)(path)
    assert response.status_code == 401
    client.headers["Authorization"] = "Bearer wrong"
    assert getattr(client, method)(path).status_code == 401


def test_launch_cookie_and_seed_routes_disabled(client, repo, drawing):
    client.headers.pop("Authorization")
    response = client.get("/__launch?t=integration-local-token", follow_redirects=False)
    assert response.status_code == 302
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/api/health").status_code == 200
    for path in ("/api/issues/seed-mock", "/api/pending/seed-mock"):
        assert client.post(path, json={"version_id": drawing["vid"]}).status_code == 404
    assert repo.list_records("issues", version_id=drawing["vid"]) == []
    assert repo.list_records("pending_confirmations", version_id=drawing["vid"]) == []


def test_configured_slot_reaches_extraction_rules_and_ledger(repo, drawing, configure, monkeypatch):
    configure()
    result, model = run_script(repo, drawing, monkeypatch,
        [payload(fact("2400mm", x=.1), fact("2500mm", x=.5)), payload(number="S-002")])
    assert len(model.requests) == 2
    assert all(r["model"] == repo.get_settings()["primary_model"]["model"] for r in model.requests)
    assert result["status"] == "已完成"
    issues = repo.list_records("issues", version_id=drawing["vid"])
    assert any(i["rule_result"]["category"] == "尺寸" and "2400" in i["title"] for i in issues)
    calls = repo.list_records("model_calls", version_id=drawing["vid"])
    assert len(calls) == 2 and all(row["input_tokens"] == 100 for row in calls)
    assert sum(row["estimated_cost_micros"] for row in calls) > 0
    assert not any((i.get("primary_opinion") or {}).get("mock") for i in issues)


def test_same_object_dimensions_across_two_sheets_are_compared(repo, drawing, configure, monkeypatch):
    configure()
    run_script(repo, drawing, monkeypatch,
        [payload(fact("2400mm")), payload(fact("2500mm"), number="S-002")])
    issues = repo.list_records("issues", version_id=drawing["vid"])
    assert any(i["rule_result"].get("category") == "尺寸" and i["left_unit_id"] != i["right_unit_id"] for i in issues), \
        "Explicitly identified XB-01 length differs across drawings but no cross-drawing dimension issue was emitted"


def test_unrelated_dimensions_on_same_row_are_not_compared(repo, drawing, configure, monkeypatch):
    configure()
    run_script(repo, drawing, monkeypatch,
        [payload(fact("2400mm", x=.1, object_id="XB-01"), fact("2500mm", x=.5, object_id="XB-02")), payload(number="S-002")])
    assert not [i for i in repo.list_records("issues", version_id=drawing["vid"]) if i["rule_result"].get("category") == "尺寸"], \
        "Screen-row proximity is not evidence that two dimensions belong to the same object"


def test_uncertain_numeric_fact_stays_out_of_comparison(repo, drawing, configure, monkeypatch):
    configure()
    run_script(repo, drawing, monkeypatch,
        [payload(fact("2400mm", x=.1, confidence=.2), fact("2500mm", x=.5)), payload(number="S-002")])
    assert repo.list_records("pending_confirmations", version_id=drawing["vid"])
    assert not [i for i in repo.list_records("issues", version_id=drawing["vid"]) if i["rule_result"].get("category") == "尺寸"], \
        "A low-confidence number awaiting human confirmation must not enter dimension comparison"


def test_recognized_title_and_out_of_scope_reach_directory_and_coverage(repo, drawing, configure, monkeypatch):
    configure()
    result, _ = run_script(repo, drawing, monkeypatch,
        [payload(number="S-099", out_of_scope=True), payload(number="S-002")])
    unit = repo.get("drawing_units", drawing["units"][0]["id"])
    assert unit["drawing_number"] == "S-099", "The task discarded extracted title-block identity"
    assert unit["metadata"].get("out_of_scope") is True
    assert result["out_of_scope_count"] >= 1


def test_failed_extraction_zero_issues_is_partial_and_exports_incomplete(repo, drawing, configure, monkeypatch):
    configure()
    result, _ = run_script(repo, drawing, monkeypatch,
        [ModelError(CODE_TIMEOUT, "controlled timeout"), payload(number="S-002")])
    assert result["issue_count"] == 0
    assert result["outcome"] == "partial"
    assert result["incomplete_count"] + result["unreadable_count"] > 0
    exported = ExportService(repo).generate(drawing["vid"])
    workbook = load_workbook(ExportService(repo).resolve(exported["files"][1]["id"]))
    assert workbook["未完成与读不了"].max_row > 1


def test_issue_summary_uses_persisted_incomplete_not_query_stub(client, repo, drawing):
    finish_task(repo, drawing, incomplete=[{"unit_id": drawing["units"][0]["id"], "reason": "识别未完成", "category": "incomplete"}])
    response = client.get("/api/issues", params={"version_id": drawing["vid"]})
    assert response.status_code == 200
    assert response.json()["incomplete_count"] >= 1, "Opening results without caller-supplied counts hid persisted incomplete coverage"


def test_budget_pause_retains_results_and_exports_without_model(repo, drawing, configure, monkeypatch):
    configure(limit=.001)
    model = ScriptedModel([payload(), payload(number="S-002")], input_tokens=1_000_000, output_tokens=1_000_000)
    monkeypatch.setattr(SettingsService, "_make_client", lambda *args: model)
    result = JobService(repo).start_review(drawing["vid"])
    assert result["status"] == "暂停" and result["cost_paused"]
    assert len(model.requests) == 1, "The second drawing must not cause another call after the cap"
    issue = make_issue(repo, drawing)
    monkeypatch.setattr(SettingsService, "_make_client", lambda *args: pytest.fail("Offline export called a model"))
    exported = ExportService(repo).generate(drawing["vid"])
    workbook = load_workbook(ExportService(repo).resolve(exported["files"][1]["id"]))
    assert workbook["问题清单"].max_row == 2
    assert workbook["未完成与读不了"].max_row > 1
    assert repo.get("issues", issue["id"])["review_status"] == "疑似"


def test_same_base_model_alias_is_rejected_before_task(repo, configure):
    configure()
    with pytest.raises(SettingsError):
        SettingsService(repo).save_settings({"review_model": {"provider": "zhipu", "model": "GLM-4.5V"}},
                                             review_api_key="test-integration-secret-review")
    assert same_base_model("siliconflow", "zai-org/GLM-4.5V", None, "zhipu", "GLM-4.5V", None)


def test_each_format_retry_is_metered_and_budget_checked(repo, drawing, configure, monkeypatch):
    configure(limit=.001)
    # A provider already consumed/charged usage even though its JSON failed schema validation.
    model = ScriptedModel([{}, payload(), payload(number="S-002")],
                          input_tokens=1_000_000, output_tokens=1_000_000)
    monkeypatch.setattr(SettingsService, "_make_client", lambda *args: model)
    result = JobService(repo).start_review(drawing["vid"])
    assert len(model.requests) == 1, "Format retry invoked the provider again after the first charged response exceeded the monthly cap"
    rows = repo.list_records("model_calls", version_id=drawing["vid"])
    assert len(rows) == 1 and rows[0]["estimated_cost_micros"] > 0
    assert result["cost_paused"] is True


def test_format_retry_keeps_usage_of_every_provider_response(repo, drawing, configure, monkeypatch):
    configure()
    _, model = run_script(repo, drawing, monkeypatch, [{}, payload(), payload(number="S-002")])
    rows = repo.list_records("model_calls", version_id=drawing["vid"])
    assert len(model.requests) == 3
    assert sum(row["input_tokens"] for row in rows) == 300, "Malformed first response usage disappeared from the call ledger"
    assert sum(row["output_tokens"] for row in rows) == 90


def test_real_import_pdf_render_rules_export_and_durable_unreadable(repo, tmp_path, configure, monkeypatch):
    import shutil
    import subprocess
    from backend.parsers.pdf import PDFParser
    from reportlab.pdfgen import canvas
    if not (shutil.which("pdftoppm") and shutil.which("pdfinfo")):
        pytest.skip("Real Poppler unavailable; no synthetic renderer is substituted for this test")
    configure()
    source = tmp_path / "自制两图.pdf"
    doc = canvas.Canvas(str(source), pagesize=(400, 300))
    for label in ("S-001: YJV 3x50", "S-002: YJV 3x70"):
        doc.drawString(40, 200, label)
        doc.showPage()
    doc.save()
    # Binary presence alone is not proof it can render in this managed runtime.
    # Keep environmental startup/font problems distinct from product assertions.
    try:
        probe = subprocess.run(["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-png", "-r", "72",
                                str(source), str(tmp_path / "poppler-probe")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    except subprocess.TimeoutExpired:
        pytest.skip("Real Poppler preflight timed out in 5s; no renderer substitute and no product verdict")
    if probe.returncode:
        pytest.skip(f"Real Poppler preflight failed with exit {probe.returncode}; no product verdict")
    corrupt = tmp_path / "损坏.pdf"
    corrupt.write_bytes(b"%PDF-1.7\nnot a valid PDF\n")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with ImportService(repo.root, pdf_parser=PDFParser(page_timeout=5, info_timeout=5)) as service:
        job = service.wait(service.submit([source, corrupt]), timeout=20)
    assert job["status"] == "completed", job
    imported = job["result"]
    assert imported["unit_count"] == 2 and len(imported["unreadable"]) == 1
    script = ScriptedModel([
        payload(fact("YJV 3x50", category="材料规格")),
        payload(fact("YJV 3x70", category="材料规格"), number="S-002")])
    monkeypatch.setattr(SettingsService, "_make_client", lambda *args: script)
    result = JobService(repo).start_review(imported["version_id"])
    findings = repo.list_records("issues", version_id=imported["version_id"])
    assert any(i["rule_result"].get("category") == "材料规格" for i in findings)
    exports = ExportService(repo).generate(imported["version_id"])
    assert len(PdfReader(ExportService(repo).resolve(exports["files"][0]["id"])).pages) == 3
    workbook = load_workbook(ExportService(repo).resolve(exports["files"][1]["id"]))
    assert any("损坏.pdf" in str(row[0]) for row in workbook["未完成与读不了"].iter_rows(values_only=True))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert result["unreadable_count"] >= 1, "The finished task lost the durable import failure and reported full coverage"
    assert result["outcome"] == "partial"


def test_import_failure_manifest_counts_toward_task_coverage(repo, drawing, configure, monkeypatch):
    configure()
    manifest = repo.root / "projects" / drawing["project"]["id"] / "cache" / f"import-{drawing['vid']}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"unreadable": [{"name": "损坏图纸.pdf", "reason": "文件损坏，读不了", "code": "PDF_CORRUPT"}]}))
    result, _ = run_script(repo, drawing, monkeypatch, [payload(), payload(number="S-002")])
    assert result["unreadable_count"] >= 1, "An imported unreadable file vanished from task coverage despite its durable manifest"
    assert result["outcome"] == "partial"


@pytest.mark.parametrize("catalog_complete", [None, True, False], ids=[
    "missing-catalog-stays-partial", "complete-catalog", "incomplete-catalog-stays-partial"])
def test_raising_budget_resumes_review_without_stale_pause_coverage(repo, drawing, configure, monkeypatch, catalog_complete):
    # Model boundary evidence, validated/persisted by the actual extraction
    # pipeline, must establish directory completeness. A seeded list alone does
    # not mean the whole catalogue was read and must never clear coverage.
    second_payload = payload(number="S-002")
    if catalog_complete is not None:
        second_payload.update(is_catalog_sheet=True, catalog_listed_numbers=["S-001"],
                              catalog_complete=catalog_complete)
    configure(dual=True)
    slot = repo.get_settings()["primary_model"]
    price = price_for_model(slot["provider"], slot["model"])
    per_call = estimate_cost_micros(input_tokens=100, output_tokens=30,
        input_price_per_m_yuan=price.input_per_m_yuan,
        output_price_per_m_yuan=price.output_per_m_yuan).estimated_cost_micros
    SettingsService(repo).save_settings({"monthly_limit_yuan": per_call * 1.5 / 1_000_000})
    primary = ScriptedModel([payload(fact("2400mm", x=.1), fact("2500mm", x=.5)), second_payload])
    reviewer = ScriptedModel([{"verdict": "agree", "explanation": "已核对两侧证据"}])
    monkeypatch.setattr(SettingsService, "_make_client",
        lambda self, model, secret: primary if model["provider"] == "siliconflow" else reviewer)
    service = JobService(repo)
    first = service.start_review(drawing["vid"])
    assert first["cost_paused"] and len(primary.requests) == 2 and not reviewer.requests
    paused = repo.get("review_tasks", first["task_id"])
    assert any(row.get("reason") == "费用上限，审查已暂停" for row in paused["incomplete"])
    SettingsService(repo).save_settings({"monthly_limit_yuan": 100})
    second = service.start_review(drawing["vid"])
    assert second["task_id"] == first["task_id"]
    assert second["status"] == "已完成" and not second["cost_paused"]
    assert len(primary.requests) == 2 and len(reviewer.requests) == 1
    resumed = repo.get("review_tasks", second["task_id"])
    assert not any("费用上限" in row.get("reason", "") for row in resumed["incomplete"]), \
        "A successfully resumed task retained an obsolete cost-pause incomplete row"
    if catalog_complete is True:
        actual = repo.get("drawing_units", drawing["units"][1]["id"])["metadata"]
        assert actual["extract_status"] == "ok" and actual["catalog_complete"] is True
        assert actual["catalog_listed_numbers"] == ["S-001"]
        assert resumed["incomplete"] == []
        assert second["outcome"] == "completed" and second["incomplete_count"] == 0
    elif catalog_complete is False:
        actual = repo.get("drawing_units", drawing["units"][1]["id"])["metadata"]
        assert actual["extract_status"] == "ok" and actual["catalog_complete"] is False
        assert any("目录" in row["reason"] for row in resumed["incomplete"])
        assert second["outcome"] == "partial" and second["incomplete_count"] > 0
    else:
        assert [row["reason"] for row in resumed["incomplete"]] == ["无目录页，目录与图签核对跳过"]
        assert second["outcome"] == "partial" and second["incomplete_count"] == 1


@pytest.mark.parametrize("verdict,status", [("agree", "completed"), ("disagree", "completed"), ("uncertain", "incomplete")])
def test_second_model_records_opinion_without_human_confirmation(repo, drawing, configure, verdict, status):
    configure(dual=True)
    issue = make_issue(repo, drawing)
    task = finish_task(repo, drawing)
    settings = repo.get_settings()
    model = ScriptedModel([{"verdict": verdict, "explanation": "controlled evidence review"}])
    result = ReviewService(repo, task_id=task["id"], primary_model=settings["primary_model"],
        review_model=settings["review_model"], client=model).run(drawing["vid"])
    row = repo.get("issues", issue["id"])
    assert row["review_status"] == "疑似"
    assert row["review_opinion"]["status"] == status
    assert result.model_calls == 1
    if verdict == "disagree":
        assert "两个模型意见不一致" in row["review_opinion"]["explanation"]
    if verdict == "uncertain":
        assert result.incomplete


def test_second_model_numeric_reading_disagreement_becomes_pending(client, repo, drawing, configure):
    configure(dual=True)
    issue = make_issue(repo, drawing)
    task = finish_task(repo, drawing)
    settings = repo.get_settings()
    model = ScriptedModel([{"verdict": "disagree", "explanation": "左侧数字读法与主模型不同",
                            "left_text": "2410 mm", "right_text": "2500 mm"}])
    service = ReviewService(repo, task_id=task["id"], primary_model=settings["primary_model"],
                            review_model=settings["review_model"], client=model)
    assert service.run(drawing["vid"]).model_calls == 1
    pending = repo.list_records("pending_confirmations", version_id=drawing["vid"])
    assert len(pending) == 1
    row = pending[0]
    assert row["status"] == "待确认" and row["reason"] == "两模型读数不一致，待确认"
    assert row["unit_id"] == issue["left_unit_id"] and row["text"] == "2400 mm"
    assert all(row[axis] == issue[f"left_{axis}"] for axis in ("x0", "y0", "x1", "y1"))
    assert repo.get("issues", issue["id"])["review_status"] == "疑似"
    response = client.get("/api/issues", params={"version_id": drawing["vid"]})
    assert response.json()["incomplete_count"] >= 1
    # Replaying cached evidence may not duplicate pending work or incur another call.
    cached = service.run(drawing["vid"])
    assert cached.model_calls == 0 and cached.cache_hits == 1 and len(model.requests) == 1
    assert len(repo.list_records("pending_confirmations", version_id=drawing["vid"])) == 1


def test_offline_review_two_real_reports_preserve_original_and_old_export(client, repo, drawing, configure, monkeypatch):
    configure()
    issue = make_issue(repo, drawing, title="=HYPERLINK(\"https://invalid.example\",\"source\")")
    finish_task(repo, drawing)
    before = hashlib.sha256(repo.original_path(drawing["original"]["id"]).read_bytes()).hexdigest()
    first = client.post(f"/api/versions/{drawing['vid']}/exports")
    assert first.status_code == 200, first.text
    files = first.json()["files"]
    pdf = client.get(files[0]["url"]).content
    xlsx = client.get(files[1]["url"]).content
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 3
    workbook = load_workbook(io.BytesIO(xlsx), data_only=False)
    assert workbook.sheetnames == ["问题清单", "未完成与读不了"]
    assert workbook["问题清单"]["E2"].data_type == "s"
    assert workbook["问题清单"]["K2"].value == "单模型"
    response = client.patch(f"/api/issues/{issue['id']}", json={"review_status": "排除", "review_note": "离线复核"})
    assert response.status_code == 200
    second = client.post(f"/api/versions/{drawing['vid']}/exports")
    assert second.status_code == 200
    assert client.get(files[0]["url"]).content == pdf
    assert client.get(files[1]["url"]).content == xlsx
    second_pdf = client.get(second.json()["files"][0]["url"]).content
    assert len(PdfReader(io.BytesIO(second_pdf)).pages) == 1, "Excluded findings must be present only in XLSX"
    assert hashlib.sha256(repo.original_path(drawing["original"]["id"]).read_bytes()).hexdigest() == before
    with zipfile.ZipFile(io.BytesIO(xlsx)) as archive:
        all_xml = b"\n".join(archive.read(name) for name in archive.namelist())
    for secret in (b"test-integration-secret-primary", b"test-integration-secret-review"):
        assert secret not in all_xml and secret not in pdf


def test_tampered_export_is_not_downloaded(client, repo, drawing):
    make_issue(repo, drawing)
    finish_task(repo, drawing)
    files = ExportService(repo).generate(drawing["vid"])["files"]
    path = ExportService(repo).resolve(files[0]["id"])
    path.write_bytes(b"tampered")
    assert client.get(files[0]["url"]).status_code == 404


@pytest.mark.parametrize("incomplete,status", [([{"reason": "识别未完成", "category": "incomplete"}], "已完成"), ([], "失败"), ([], "暂停")])
def test_partial_revision_never_marks_old_issue_resolved(repo, drawing, incomplete, status):
    make_issue(repo, drawing, status="已确认")
    revision = RevisionService(repo).create_revision(drawing["vid"])
    new_original = repo.import_original(revision["version_id"], drawing["source"])["record"]
    repo.create_unit(revision["version_id"], new_original["id"], kind="pdf_page", ordinal=1,
                     name="基础图1", drawing_number="S-001", metadata={})
    RevisionService(repo).prepare(revision["version_id"])
    revised = {**drawing, "vid": revision["version_id"]}
    task = finish_task(repo, revised, incomplete=incomplete, status=status)
    comparison = RevisionService(repo).finalize(revised["vid"], task["id"])
    assert comparison["comparison_complete"] is False
    assert comparison["counts"]["已解决"] == 0
    assert comparison["unresolved_previous_count"] == 1


@pytest.mark.parametrize("change", ["inherit", "replace", "remove"])
def test_revision_scope_coverage_uses_only_final_drawing_units(
        change, client, repo, drawing, configure, monkeypatch, tmp_path):
    from PIL import Image
    from pypdf import PdfWriter
    from backend.drawings import DrawingRemovalService

    configure()
    initial, _ = run_script(repo, drawing, monkeypatch,
        [payload(number="S-001", out_of_scope=True), payload(number="S-002")])
    assert initial["out_of_scope_count"] == 1
    old_scope_id = drawing["units"][0]["id"]
    if change == "remove":
        version_id = DrawingRemovalService(repo).remove(drawing["vid"], unit_ids=[old_scope_id])["version_id"]
        responses = []
    else:
        version_id = RevisionService(repo).create_revision(drawing["vid"])["version_id"]
        number = "S-002" if change == "inherit" else "S-001"
        source = tmp_path / "replacement.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=600, height=300)
        writer.write(source)
        original = repo.import_original(version_id, source)["record"]
        image = repo.root / "replacement.png"
        Image.new("RGB", (600, 300), "blue").save(image)
        repo.create_unit(version_id, original["id"], kind="pdf_page", ordinal=1,
            name="修订页", drawing_number=number, metadata={"review_relative_path": image.name})
        responses = [payload(number=number)]
    if change == "inherit":
        path = repo.root / f"projects/{drawing['project']['id']}/cache/import-{version_id}.json"
        path.write_text(json.dumps({"unreadable": [{"name": "备注.txt", "reason": "不支持的格式"}]}))

    result, model = run_script(repo, {"vid": version_id}, monkeypatch, responses)
    assert len(model.requests) == len(responses)
    assert result["status"] == "已完成"
    expected = int(change == "inherit")
    assert result["out_of_scope_count"] == expected
    bundle = client.get("/api/issues", params={"version_id": version_id}).json()
    assert bundle["out_of_scope_count"] == expected
    assert bundle["unreadable_count"] == expected
    assert result["outcome"] == bundle["outcome"]
    assert result["outcome"] == ("partial" if expected or bundle["incomplete_count"] else "completed")
    scope_rows = [row for row in bundle["incomplete_items"] if row["category"] == "out_of_scope"]
    assert len(scope_rows) == expected
    if expected:
        final_id = RevisionService(repo).prepare(version_id)["old_to_new"][old_scope_id]
        assert final_id != old_scope_id
        assert [(row["unit_id"], row["drawing_number"], row["reason"]) for row in scope_rows] == [
            (final_id, "S-001", "不在审查范围")]

    exporter = ExportService(repo)
    export = exporter.generate(version_id)
    book = load_workbook(exporter.resolve(export["files"][1]["id"]))
    coverage = list(book["未完成与读不了"].values)[1:]
    assert [row for row in coverage if row[1] == "不在审查范围"] == [("S-001", "不在审查范围")] * expected
    if expected:
        assert any("不支持的格式" in row[1] for row in coverage)


def test_scope_count_and_export_keep_distinct_units_with_the_same_drawing_number(
        repo, drawing, configure, monkeypatch):
    configure()
    result, _ = run_script(repo, drawing, monkeypatch,
        [payload(number="S-099", out_of_scope=True), payload(number="S-099", out_of_scope=True)])
    assert result["out_of_scope_count"] == 2
    task = repo.get("review_tasks", result["task_id"])
    assert {row["unit_id"] for row in task["incomplete"] if row["category"] == "out_of_scope"} == {
        unit["id"] for unit in drawing["units"]}
    exporter = ExportService(repo)
    export = exporter.generate(drawing["vid"])
    book = load_workbook(exporter.resolve(export["files"][1]["id"]))
    assert [row for row in book["未完成与读不了"].values if row[1] == "不在审查范围"] == [
        ("S-099", "不在审查范围"), ("S-099", "不在审查范围")]
