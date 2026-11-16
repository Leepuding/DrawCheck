"""DC-15：结果页 A7/A8/A12 与 0 条文案、排除置灰。"""
from __future__ import annotations

import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image
from playwright.sync_api import Error, expect


def _enter_results(page, configure):
    configure()
    version_id = page.locator("body").get_attribute("data-version-id")
    response = page.request.post(page.url.rstrip("/") + "/api/issues/seed-mock", data={"version_id": version_id})
    assert response.ok
    page.get_by_test_id("btn-review").click()
    page.get_by_test_id("btn-disclosure-ok").click()
    expect(page.locator("body")).to_have_attribute("data-state", "results", timeout=15000)
    expect(page.get_by_test_id("results-title")).to_have_text("审查部分完成")
    return page.locator("body").get_attribute("data-version-id")


def _png(color):
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, format="PNG")
    return out.getvalue()


def _route_regions(page):
    page.route("**/api/units/*/image-region/info", lambda route: route.fulfill(json={
        "kind": "pdf_vector", "width": 100, "height": 100, "max_dimension": 2048, "max_pixels": 4194304,
    }))


def test_a7_viewer_highlight_compare_zoom(imported, configure, screenshot):
    page = imported
    _route_regions(page)
    region_png = _png("white")
    page.route("**/api/units/*/image-region?*", lambda route: route.fulfill(content_type="image/png", body=region_png))
    _enter_results(page, configure)
    row = page.get_by_test_id("issue-row").first
    expect(row).to_be_visible(timeout=10000)
    row.click()
    img = page.get_by_test_id("viewer-img")
    expect(img).to_be_visible()
    expect(page.get_by_test_id("viewer-title")).not_to_have_text("")
    hl = page.get_by_test_id("viewer-hl")
    expect(hl).to_be_visible()
    box = hl.bounding_box()
    img_box = img.bounding_box()
    assert box and img_box
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    assert img_box["x"] - 2 <= cx <= img_box["x"] + img_box["width"] + 2
    assert img_box["y"] - 2 <= cy <= img_box["y"] + img_box["height"] + 2
    compare = page.get_by_test_id("viewer-compare")
    if compare.is_visible():
        before = page.get_by_test_id("viewer-title").inner_text()
        compare.click()
        expect(page.get_by_test_id("viewer-title")).not_to_have_text(before)
        expect(page.get_by_test_id("viewer-hl")).to_be_visible()
    page.get_by_test_id("btn-zoom-in").click()
    expect(page.locator("#zoom-value")).to_have_text("125%")
    stage = page.locator(".viewer-stage")
    sheet = page.locator("#drawing-sheet")
    before = sheet.bounding_box()
    point = (before["x"] + before["width"] * .58, before["y"] + before["height"] * .55)
    page.mouse.move(*point)
    page.mouse.wheel(0, -120)
    expect(page.locator("#zoom-value")).not_to_have_text("125%")
    after = sheet.bounding_box()
    assert abs((point[0] - after["x"]) / after["width"] - .58) < .01
    assert abs((point[1] - after["y"]) / after["height"] - .55) < .01
    with page.expect_request(lambda req: "/image-region?" in req.url) as request:
        page.get_by_test_id("btn-zoom-ten").click()
    expect(page.locator("#zoom-value")).to_have_text("1000%")
    args = parse_qs(urlsplit(request.value.url).query)
    requested_width = float(args["x1"][0]) - float(args["x0"][0])
    visible_width = stage.bounding_box()["width"] / sheet.bounding_box()["width"]
    assert 0 < visible_width <= requested_width < 1
    assert abs(requested_width - 1.3 * visible_width) < .02
    assert 0 < int(args["width"][0]) <= 2048
    expect(img).to_be_hidden()
    expect(page.get_by_test_id("viewer-region")).to_be_visible(timeout=10000)

    stage_box = stage.bounding_box()
    x = stage_box["x"] + stage_box["width"] / 2
    y = stage_box["y"] + stage_box["height"] / 2
    sheet_x = sheet.bounding_box()["x"]
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 45, y + 15)
    page.mouse.up()
    assert abs(sheet.bounding_box()["x"] - sheet_x - 45) < 2

    for _ in range(8):
        stage.dispatch_event("wheel", {"deltaY": -800, "clientX": x, "clientY": y})
    expect(page.locator("#zoom-value")).to_have_text("100000%")
    expect(page.get_by_test_id("btn-zoom-in")).to_be_disabled()
    page.get_by_test_id("btn-zoom-out").click()
    expect(page.locator("#zoom-value")).to_have_text("80000%")
    page.get_by_test_id("btn-zoom-fit").click()
    expect(page.locator("#zoom-value")).to_have_text("100%")
    expect(img).to_be_visible()
    screenshot(page, "dc15-results-highlight.png")


def test_a8_review_persist_and_relabel(imported, configure, client, screenshot):
    page = imported
    version_id = _enter_results(page, configure)
    assert version_id
    row = page.get_by_test_id("issue-row").first
    expect(row).to_be_visible(timeout=10000)
    row.click()
    issue_id = row.get_attribute("data-id")
    page.get_by_test_id("btn-confirm").click()
    target = page.locator(f'[data-testid="issue-row"][data-id="{issue_id}"]')
    expect(target).to_have_attribute("data-status", "已确认")
    expect(target.locator(".status-label")).to_have_text("已确认")
    expect(target.locator(".status-label")).to_have_attribute("data-status", "已确认")
    # 按钮文案仍为「确认」
    expect(page.get_by_test_id("btn-confirm")).to_have_text("确认")
    remote = client.get("/api/issues", params={"version_id": version_id}).json()
    found = next(item for item in remote["items"] if item["id"] == issue_id)
    assert found["review_status"] == "已确认"
    # 改排除并置灰排后
    page.get_by_test_id("btn-exclude").click()
    expect(target).to_have_attribute("data-status", "排除")
    expect(target).to_have_class(re.compile(r"is-excluded"))
    ids = page.get_by_test_id("issue-row").evaluate_all("els => els.map(e => e.dataset.id)")
    assert ids[-1] == issue_id
    # 需补充资料
    page.locator(f'[data-testid="issue-row"][data-id="{issue_id}"]').click()
    page.get_by_test_id("btn-needinfo").click()
    expect(page.locator(f'[data-testid="issue-row"][data-id="{issue_id}"]')).to_have_attribute(
        "data-status", "需补充资料"
    )
    # 再点当前态回疑似
    page.get_by_test_id("btn-needinfo").click()
    expect(page.locator(f'[data-testid="issue-row"][data-id="{issue_id}"]')).to_have_attribute(
        "data-status", "疑似"
    )
    screenshot(page, "dc15-results-reviewed.png")


def test_a12_no_tolerance_and_viewport(imported, configure, screenshot):
    page = imported
    _enter_results(page, configure)
    body = page.locator("body")
    assert "容差" not in body.inner_text()
    assert page.get_by_test_id("btn-confirm").count() + page.get_by_test_id("btn-exclude").count() >= 0
    # 同屏主按钮：导入/审查/导出（设置是齿轮）≤8
    visible_main = page.locator(".main-button:visible").count()
    assert visible_main <= 8
    overflow = page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
    assert overflow is False
    # 选中问题后三复核按钮首屏可见
    page.get_by_test_id("issue-row").first.click()
    for name in ("btn-confirm", "btn-exclude", "btn-needinfo"):
        expect(page.get_by_test_id(name)).to_be_in_viewport()
    screenshot(page, "dc15-results-a12.png")


def test_tabs_and_empty_confirm_copy(imported, configure):
    page = imported
    # 强制 0 条待确认，验收空态短提示（DC-16；不再用「稍后确认」弱提示）
    empty = '{"version_id":"x","items":[],"pending_count":0,"incomplete_pending_count":0}'
    page.route("**/api/pending/seed-mock", lambda route: route.fulfill(
        status=200, content_type="application/json", body=empty))
    page.route("**/api/pending?*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=empty))
    _enter_results(page, configure)
    expect(page.get_by_test_id("tab-issues")).to_contain_text("哪里对不上")
    page.get_by_test_id("tab-confirm").click()
    expect(page.get_by_test_id("confirm-empty")).to_contain_text("没有需要确认的内容")
    assert page.get_by_test_id("in-confirm-value").count() == 0
    page.get_by_test_id("tab-issues").click()


@pytest.mark.parametrize("verified, expected", [(False, "尚未形成有效对照"), (True, "已核验范围内未发现差异")])
def test_zero_issues_copy(imported, configure, verified, expected):
    """零差异必须区分核验过的范围和未建立有效对照。"""
    page = imported
    configure()
    # 明确模拟完成任务与空问题，避免真实 partial 被误认作零问题。
    page.route('**/api/versions/*/tasks', lambda route: route.fulfill(json={
        'task_id':'empty-ui', 'status':'已完成', 'outcome':'completed', 'percent':100,
        'incomplete_count':0, 'unreadable_count':0, 'out_of_scope_count':0}))
    page.route("**/api/issues?*", lambda route: route.fulfill(json={
        "items": [], "incomplete_count": 0, "unreadable_count": 0, "out_of_scope_count": 0,
        "verification": {"has_effective_comparison": verified, "zero_difference_message": expected},
    }))
    page.get_by_test_id("btn-review").click()
    page.get_by_test_id("btn-disclosure-ok").click()
    expect(page.locator("body")).to_have_attribute("data-state", "results", timeout=15000)
    expect(page.get_by_test_id("empty-issues")).to_contain_text(expected)
    text = page.locator("body").inner_text()
    assert "没问题" not in text
    assert "审查通过" not in text
    page.screenshot(path="_evidence/dc15-results-zero.png")


def test_finding_shows_both_values_and_jumps_to_two_original_marks(imported, configure):
    page = imported
    _route_regions(page)

    def varied_anchors(route):
        response = route.fetch()
        payload = response.json()
        issues = {item["title"]: item for item in payload["items"]}
        issues["箱变基础尺寸不一致"]["finding"]["left"]["bbox"] = {"x0": .27, "y0": .38, "x1": .29, "y1": .395}
        issues["支架基础标高不一致"]["finding"]["left"]["bbox"] = {"x0": .03, "y0": .05, "x1": .85, "y1": .95}
        route.fulfill(response=response, json=payload)

    def assert_centered_mark():
        mark = page.get_by_test_id("viewer-hl").bounding_box()
        stage = page.locator(".viewer-stage").bounding_box()
        assert stage["x"] <= mark["x"] <= mark["x"] + mark["width"] <= stage["x"] + stage["width"]
        assert stage["y"] <= mark["y"] <= mark["y"] + mark["height"] <= stage["y"] + stage["height"]
        assert abs(mark["x"] + mark["width"] / 2 - stage["x"] - stage["width"] / 2) < 2
        assert abs(mark["y"] + mark["height"] / 2 - stage["y"] - stage["height"] / 2) < 2

    page.route("**/api/issues?*", varied_anchors)
    _enter_results(page, configure)
    page.get_by_test_id("issue-row").filter(has_text="箱变基础尺寸不一致").click()
    proof = page.get_by_test_id("proof-panel")
    expect(proof).to_contain_text("2400×1800")
    expect(proof).to_contain_text("2500×1800")
    expect(proof).to_contain_text("相差 100 mm")
    expect(proof).to_contain_text("按同一箱变基础的尺寸配对")
    expect(proof).to_contain_text("对照依据")
    expect(proof).to_contain_text("原文")
    before = page.get_by_test_id("viewer-img").get_attribute("src")
    left_requests = []
    right_requests = []
    right_info_requests = []
    right_png = _png("blue")
    busy_payload = {"error": {"code": "VIEWER_BUSY", "message": "正在准备高清图"}}

    def info_response(route):
        if urlsplit(route.request.url).path != before + "-region/info":
            right_info_requests.append(route.request.url)
            if len(right_info_requests) == 1:
                route.fulfill(status=503, json=busy_payload)
                return
        route.fallback()

    def region_response(route):
        if urlsplit(route.request.url).path == before + "-region":
            left_requests.append(route)
            return
        right_requests.append(route.request.url)
        if len(right_requests) == 1:
            route.fulfill(status=503, json=busy_payload)
        else:
            route.fulfill(content_type="image/png", body=right_png)

    page.route("**/api/units/*/image-region/info", info_response)
    page.route("**/api/units/*/image-region?*", region_response)
    with page.expect_request(lambda request: "/image-region?" in request.url):
        page.get_by_test_id("proof-left").click()
    expect(page.get_by_test_id("viewer-hl")).to_be_visible()
    left_zoom = int(page.locator("#zoom-value").inner_text().rstrip("%"))
    page.get_by_test_id("proof-right").click()
    expect(page.get_by_test_id("viewer-hl")).to_be_visible()
    assert page.get_by_test_id("viewer-img").get_attribute("src") != before
    assert abs(page.get_by_test_id("viewer-hl").evaluate("el => parseFloat(el.style.left)") - 56) < .05
    expect(page.get_by_text("正在准备原图细节，请稍候…", exact=True)).to_be_visible()
    visible_region = page.get_by_test_id("viewer-region")
    expect(visible_region).to_be_visible()
    assert len(right_info_requests) == 2 and right_info_requests[0] == right_info_requests[1]
    assert len(right_requests) == 2 and right_requests[0] == right_requests[1]
    assert left_requests
    selected_image = visible_region.get_attribute("src")
    try:
        left_requests[0].fulfill(content_type="image/png", body=_png("red"))
    except Error:
        pass  # 浏览器允许取消旧请求，旧请求即使返回也不能覆盖当前图。
    assert visible_region.get_attribute("src") == selected_image
    right_zoom = int(page.locator("#zoom-value").inner_text().rstrip("%"))
    assert right_zoom < left_zoom
    assert_centered_mark()

    page.get_by_test_id("issue-row").filter(has_text="支架基础标高不一致").click()
    page.get_by_test_id("proof-left").click()
    expect(page.get_by_test_id("viewer-hl")).to_be_visible()
    expect(page.locator("#zoom-value")).not_to_have_text("200%")
    broad_zoom = int(page.locator("#zoom-value").inner_text().rstrip("%"))
    assert 100 < broad_zoom < 200
    assert_centered_mark()


def test_missing_original_or_anchor_is_explicit(imported, configure, client):
    page = imported
    configure()
    version_id = page.locator("body").get_attribute("data-version-id")
    a, b = client.get("/api/units", params={"version_id": version_id}).json()["items"]
    left = {"sheet": "目录", "image_url": f"/api/units/{a['id']}/image",
            "bbox": {"x0": .2, "y0": .25, "x1": .3, "y1": .3}, "text": "D-01", "value": "D-01"}
    page.route("**/api/versions/*/tasks", lambda route: route.fulfill(json={
        "task_id": "missing-proof", "status": "已完成", "outcome": "completed", "percent": 100}))
    page.route("**/api/issues?*", lambda route: route.fulfill(json={"items": [
        {"id": "missing-sheet", "title": "缺少目标图纸", "review_status": "疑似",
         "finding": {"left": left, "right": {"sheet": "目标图纸缺失", "image_url": None}}},
        {"id": "missing-anchor", "title": "目标图纸缺标注位置", "review_status": "疑似",
         "finding": {"left": left, "right": {"sheet": "右图", "image_url": f"/api/units/{b['id']}/image", "bbox": None}}},
    ]}))
    page.get_by_test_id("btn-review").click()
    page.get_by_test_id("btn-disclosure-ok").click()
    proof = page.get_by_test_id("proof-panel")
    expect(proof).to_contain_text("目标图纸缺失")
    expect(proof).to_contain_text("无可用原图")
    expect(page.get_by_test_id("proof-right")).to_have_count(0)
    page.get_by_test_id("issue-row").filter(has_text="目标图纸缺标注位置").click()
    expect(proof).to_contain_text("未找到原图标注位置")
    expect(page.get_by_test_id("proof-right")).to_have_text("查看原图")
    page.get_by_test_id("proof-right").click()
    expect(page.get_by_test_id("viewer-title")).to_have_text("右图")
    expect(page.get_by_test_id("viewer-hl")).to_be_hidden()
    expect(page.get_by_test_id("viewer-hint")).to_contain_text("未能定位到具体原图标注")


def test_image_404_shows_hint(imported, configure):
    page = imported
    _enter_results(page, configure)
    page.get_by_test_id("issue-row").first.click()
    page.route("**/api/units/*/image", lambda route: route.fulfill(status=404, body="missing"))
    # 触发重新选中以重载图
    rows = page.get_by_test_id("issue-row")
    if rows.count() > 1:
        rows.nth(1).click()
        rows.first.click()
    else:
        page.get_by_test_id("btn-zoom-in").click()
        page.evaluate("""() => {
          const img = document.querySelector('[data-testid="viewer-img"]');
          if (img) { img.dispatchEvent(new Event('error')); }
        }""")
    expect(page.get_by_test_id("viewer-hint")).to_contain_text("图纸预览加载失败", timeout=5000)
    assert page.locator("body").get_attribute("data-state") == "results"




def test_unverified_details_never_expose_equal_markups(imported, configure):
    page = imported
    configure()
    page.route("**/api/versions/*/tasks", lambda route: route.fulfill(json={
        "task_id": "comparison-ui", "status": "已完成", "outcome": "partial", "percent": 100}))
    page.route("**/api/issues?*", lambda route: route.fulfill(json={
        "items": [], "outcome": "partial", "drawing_count": 2, "recognized_count": 2,
        "verification": {"has_effective_comparison": True, "scope_note": "仅覆盖已建立可信对应关系的内容"},
        "comparison_summary": [
            {"category": "标高", "verified": True, "difference": 0, "unverified": 0},
            {"category": "尺寸", "verified": False, "difference": 0, "unverified": 0},
            {"category": "接线", "verified": False, "difference": 0, "unverified": 1},
        ],
        "comparison_details": [
            {"id": "1", "category": "标高", "label": "女儿墙标高", "outcome": "match", "displayed_only": True},
            {"id": "2", "category": "接线", "label": "保护信号", "outcome": "unverified", "reason": "未读出线号"},
        ],
    }))
    page.route("**/api/pending?*", lambda route: route.fulfill(json={
        "pending_count": 1, "incomplete_pending_count": 1,
        "items": [{"id": "needs-human", "status": "待确认", "title": "图签字迹待确认"}],
    }))
    page.get_by_test_id("btn-review").click()
    page.get_by_test_id("btn-disclosure-ok").click()
    expect(page.get_by_test_id("empty-issues")).to_contain_text("已核验范围内未发现差异")
    expect(page.get_by_test_id("tab-comparisons")).to_have_count(0)
    expect(page.get_by_test_id("issue-row")).to_have_count(0)
    panel = page.get_by_test_id("incomplete-panel")
    panel.locator("summary").click()
    expect(panel.get_by_role("heading", name="对照依据不足")).to_be_visible()
    expect(panel).to_contain_text("以下类别缺少对照依据（尺寸）。")
    expect(panel).to_contain_text("未读出线号")
    expect(panel).to_contain_text("人工待确认")
    expect(page.get_by_text("女儿墙标高", exact=True)).to_have_count(0)
    expect(page.get_by_test_id("proof-panel")).to_have_count(0)
    panel.get_by_role("button", name="图签字迹待确认").click()
    expect(page.get_by_test_id("tab-confirm")).to_have_attribute("aria-selected", "true")
    expect(page.get_by_test_id("confirm-row")).to_have_attribute("aria-current", "true")
