"""部分完成时展示未完成明细与无问题时图纸查看。"""
from playwright.sync_api import expect


def test_partial_result_explains_failures_and_can_view_drawings(imported, configure, screenshot):
    page = imported
    configure()
    page.route('**/api/versions/*/tasks', lambda route: route.fulfill(json={
        'task_id':'partial-ui', 'status':'已完成', 'outcome':'partial', 'percent':100,
        'incomplete_count':24, 'unreadable_count':0, 'out_of_scope_count':1}))
    page.route('**/api/issues?*', lambda route: route.fulfill(json={
        'items':[], 'incomplete_count':24, 'unreadable_count':0, 'out_of_scope_count':1,
        'pending_count':0, 'cost_paused':False, 'revised':False,
        'incomplete_items':[{'drawing_number':'S-001','drawing_name':'平面图','phase':'识别', 'reason':'模型调用超时'}]}))
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.get_by_test_id('results-title')).to_have_text('审查部分完成', timeout=10000)
    expect(page.get_by_text('另有 1 张图不在审查范围，未检查', exact=True)).to_be_visible()
    assert '未发现不一致' not in page.locator('body').inner_text()
    panel = page.get_by_test_id('incomplete-panel')
    panel.locator('summary').click()
    expect(panel).to_contain_text('S-001 · 平面图')
    expect(panel).to_contain_text('识别 · 模型调用超时')
    expect(panel.get_by_role('button', name='重新审查')).to_be_visible()
    screenshot(page, 'dc-partial-incomplete.png')
    page.get_by_test_id('tab-drawings').click()
    expect(page.get_by_test_id('drawing-view-row')).to_have_count(2)
    expect(page.get_by_test_id('viewer-img')).to_be_visible()
    page.get_by_test_id('drawing-view-row').nth(1).click()
    expect(page.get_by_test_id('viewer-title')).to_contain_text('第 2 页')
    page.get_by_test_id('tab-issues').click()
    page.get_by_test_id('incomplete-panel').locator('summary').click()
    page.get_by_test_id('incomplete-panel').get_by_role('button', name='重新审查').click()
    expect(page.get_by_test_id('dlg-disclosure')).to_be_visible()


def test_actual_partial_task_details_match_api_and_restore(imported, configure, client):
    page = imported
    configure()
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.get_by_test_id('results-title')).to_have_text('审查部分完成', timeout=30000)
    version_id = page.locator('body').get_attribute('data-version-id')
    payload = client.get('/api/issues', params={'version_id':version_id}).json()
    assert payload['incomplete_items']
    item = payload['incomplete_items'][0]
    panel = page.get_by_test_id('incomplete-panel')
    panel.locator('summary').click()
    expect(panel).to_contain_text(item['reason'])
    expect(panel).to_contain_text(item['phase'])
    page.reload(wait_until='networkidle')
    expect(page.get_by_test_id('results-title')).to_have_text('审查部分完成', timeout=10000)
    page.get_by_test_id('incomplete-panel').locator('summary').click()
    expect(page.get_by_test_id('incomplete-panel')).to_contain_text(item['reason'])


def test_actual_zero_recognition_is_unfinished(imported, configure, client, monkeypatch):
    from backend.models.mock import MockModelClient
    from backend.settings.service import SettingsService
    monkeypatch.setattr(SettingsService, '_make_client', lambda self, slot, secret: MockModelClient(branch='bad_format'))
    page = imported
    configure()
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.get_by_test_id('results-title')).to_have_text('审查未完成', timeout=30000)
    version_id = page.locator('body').get_attribute('data-version-id')
    result = client.get('/api/issues', params={'version_id':version_id}).json()
    assert result['drawing_count'] > 0 and result['recognized_count'] == 0
    assert result['incomplete_items']
    page.get_by_test_id('incomplete-panel').locator('summary').click()
    expect(page.get_by_test_id('incomplete-panel')).to_contain_text(result['incomplete_items'][0]['reason'])
    page.get_by_test_id('tab-drawings').click()
    expect(page.get_by_test_id('viewer-img')).to_be_visible()


def test_drawing_tab_ignores_legacy_local_reading_evidence(imported, configure, client):
    page = imported
    configure()
    version_id = page.locator("body").get_attribute("data-version-id")
    unit = client.get("/api/units", params={"version_id": version_id}).json()["items"][0]
    page.route("**/api/versions/*/tasks", lambda route: route.fulfill(json={
        "task_id": "local-ocr-ui", "status": "已完成", "outcome": "partial", "percent": 100}))
    page.route("**/api/issues?*", lambda route: route.fulfill(json={
        "items": [], "outcome": "partial", "drawing_count": 2, "recognized_count": 1,
        "local_readings": {unit["id"]: {"materials": {"rows": [
            {"section": {"text": "升压站"}, "fields": {"name": {"text": "光缆熔纤盒"},
             "quantity": {"text": "4", "bbox": {"x0": .6, "y0": .3, "x1": .64, "y1": .35}},
             "unit": {"text": "台"}, "remarks": {"text": "其中2台利旧"}}}
        ]}}}}))
    page.get_by_test_id("btn-review").click()
    page.get_by_test_id("btn-disclosure-ok").click()
    page.get_by_test_id("tab-drawings").click()
    expect(page.get_by_test_id("drawing-view-row")).to_have_count(2)
    expect(page.get_by_test_id("viewer-img")).to_be_visible()
    expect(page.get_by_text("本图识读证据（1 项）", exact=True)).to_have_count(0)
    expect(page.get_by_text("光缆熔纤盒", exact=False)).to_have_count(0)
    expect(page.get_by_test_id("viewer-hl")).to_be_hidden()


def test_single_page_retry_displays_title_status_and_only_sends_selected_page(imported, configure, client, monkeypatch):
    import base64
    import json
    from hashlib import sha256

    import httpx

    from backend.jobs import JobService, STEPS
    from backend.models.openai_compat import OpenAICompatClient
    from backend.settings.service import SettingsService
    from backend.store import Repository
    from backend.store.paths import safe_path

    configure(dual=True)
    page = imported
    version_id = page.locator("body").get_attribute("data-version-id")
    with Repository() as repo:
        target, other = repo.list_records("drawing_units", version_id=version_id)
        meta = {**target["metadata"], "extract_status": "incomplete", "title_drawing_number": "D0103-04",
                "manual_drawing_number_confirmed": True,
                "extract_incomplete_reason": "模型调用失败：TIMEOUT: 等待模型响应超时"}
        repo._update("drawing_units", target["id"], {"metadata_json": json.dumps(meta), "drawing_number": "D0103-04"})
        expected_sha = sha256(safe_path(repo.root, meta.get("review_relative_path") or meta["preview_relative_path"]).read_bytes()).hexdigest()
        repo._update("drawing_units", other["id"], {"metadata_json": json.dumps({**other["metadata"], "extract_status": "ok"})})
        route = JobService(repo)._model_route(repo.get_settings())
        checkpoints = {step: {"state": "done"} for step in STEPS}
        checkpoints["_meta"] = {"model_route": route, "semantic_disclosed": True}
        task = repo.create_task(version_id, checkpoints=checkpoints)
        repo.update_task(task["id"], status="已完成", checkpoints=checkpoints, estimated_cost_micros=0, incomplete=[
            {"step": "extract", "category": "incomplete", "unit_id": target["id"], "reason": meta["extract_incomplete_reason"]},
            {"step": "rules_a", "category": "incomplete", "unit_id": target["id"],
             "reason": "图纸身份尚未核实，目录缺图与张数核对未完成"},
        ])
    sends = []

    def handler(request):
        body = json.loads(request.content)
        image = body["messages"][1]["content"][0]["image_url"]["url"]
        sends.append((body.get("stream"), sha256(base64.b64decode(image.split(",", 1)[1])).hexdigest()))
        response = {"title_blocks": [{"drawing_number": "D0103-04", "name": "演示图", "confidence": .99,
                                     "bbox": {"x0": .8, "y0": .8, "x1": 1, "y1": 1}}], "facts": []}
        event = {"choices": [{"delta": {"content": json.dumps(response)}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 3122, "completion_tokens": 100}}
        return httpx.Response(200, text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")

    def make_client(self, slot, secret):
        assert slot["provider"] == "siliconflow"
        return OpenAICompatClient(provider="siliconflow", base_url="https://drawcheck-test.invalid/v1",
                                  api_key="test-only", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(SettingsService, "_make_client", make_client)

    # Open the saved result without resubmitting the entire review.
    page.reload(wait_until="networkidle")
    expect(page.locator("body")).to_have_attribute("data-state", "results", timeout=15000)
    panel = page.get_by_test_id("incomplete-panel")
    if panel.get_attribute("open") is None:
        panel.locator("summary").click()
    expect(panel).to_contain_text("图号已核实")
    expect(panel).to_contain_text("视觉识别未完成")
    expect(panel).not_to_contain_text("图纸身份尚未核实，目录缺图与张数核对未完成")
    panel.get_by_role("button", name="仅续审此页").click()
    disclosure = page.get_by_test_id("dlg-disclosure")
    expect(disclosure).to_contain_text("「D0103-04」这一张图")
    expect(disclosure).not_to_contain_text("语义分析")
    page.get_by_test_id("btn-disclosure-cancel").click()
    assert not sends
    panel.get_by_role("button", name="仅续审此页").click()
    page.get_by_test_id("btn-disclosure-ok").click()
    expect(page.locator("body")).to_have_attribute("data-state", "results", timeout=30000)
    assert sends == [(True, expected_sha)]
    result = client.get("/api/issues", params={"version_id": version_id}).json()
    assert result["recognized_count"] == 2
    assert not any(r["retryable"] for r in result["incomplete_items"])
    assert not any(r["unit_id"] == target["id"] and r["phase"] == "视觉识别未完成" for r in result["incomplete_items"])
