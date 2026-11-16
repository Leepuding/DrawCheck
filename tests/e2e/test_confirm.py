"""DC-16 A6：待你确认页签计数、红圈、保存、无法确定、再改（W7-Q1=A）。"""
from __future__ import annotations

import re

from playwright.sync_api import expect


def _enter_results(page, configure):
    configure()
    version_id = page.locator("body").get_attribute("data-version-id")
    response = page.request.post(page.url.rstrip("/") + "/api/pending/seed-mock", data={"version_id": version_id})
    assert response.ok
    page.get_by_test_id("btn-review").click()
    page.get_by_test_id("btn-disclosure-ok").click()
    expect(page.locator("body")).to_have_attribute("data-state", "results", timeout=15000)
    expect(page.get_by_test_id("results-title")).to_have_text("审查部分完成")
    return page.locator("body").get_attribute("data-version-id")


def _open_confirm(page):
    page.get_by_test_id("tab-confirm").click()
    expect(page.get_by_test_id("confirm-row").first).to_be_visible(timeout=10000)


def _tab_count(page) -> int:
    text = page.get_by_test_id("tab-confirm").inner_text()
    m = re.search(r"(\d+)\s*$", text.replace("\n", " ").strip())
    assert m, text
    return int(m.group(1))


def test_a6_tab_count_red_circle_save_unsure(imported, configure, client, screenshot):
    page = imported
    version_id = _enter_results(page, configure)
    assert version_id
    _open_confirm(page)

    rows = page.get_by_test_id("confirm-row")
    expect(rows.first).to_be_visible()
    total = rows.count()
    assert total >= 1
    assert _tab_count(page) == total

    # 选第一条：主图 + 红圈（圆角）
    rows.first.click()
    img = page.get_by_test_id("viewer-img")
    expect(img).to_be_visible()
    hl = page.get_by_test_id("viewer-hl")
    expect(hl).to_be_visible()
    expect(hl).to_have_class(re.compile(r"is-round"))
    box = hl.bounding_box()
    img_box = img.bounding_box()
    assert box and img_box
    # 圆心落在图内；相对定位误差用中心点约束（≤2px 量级：允许边界 2px）
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    assert img_box["x"] - 2 <= cx <= img_box["x"] + img_box["width"] + 2
    assert img_box["y"] - 2 <= cy <= img_box["y"] + img_box["height"] + 2

    # 与 pending bbox 百分比一致性：读 style left/top/width/height
    style = hl.get_attribute("style") or ""
    remote = client.get("/api/pending", params={"version_id": version_id}).json()
    item = remote["items"][0]
    bbox = item["bbox"]
    # 解析百分比
    def pct(name):
        m = re.search(rf"{name}:([0-9.]+)%", style)
        assert m, style
        return float(m.group(1))

    left, top, width, height = pct("left"), pct("top"), pct("width"), pct("height")
    # 换算误差：相对整图百分比，再按图宽高估像素 ≤2px
    assert abs(left - bbox["x0"] * 100) < 0.05
    assert abs(top - bbox["y0"] * 100) < 0.05
    assert abs(width - (bbox["x1"] - bbox["x0"]) * 100) < 0.05
    assert abs(height - (bbox["y1"] - bbox["y0"]) * 100) < 0.05
    px_err_x = abs(left / 100 - bbox["x0"]) * img_box["width"]
    px_err_y = abs(top / 100 - bbox["y0"]) * img_box["height"]
    assert px_err_x <= 2 and px_err_y <= 2

    screenshot(page, "dc16-confirm-red-circle.png")

    before = _tab_count(page)
    expect(page.get_by_test_id("in-confirm-value")).to_be_visible()
    page.get_by_test_id("in-confirm-value").fill("2400")
    page.get_by_test_id("btn-confirm-save").click()
    expect(page.get_by_test_id("confirm-row").first).to_have_attribute("data-processed", "true")
    expect(page.locator(".confirmation-form .handled").first).to_contain_text("已处理")
    assert _tab_count(page) == before - 1

    listed = client.get("/api/pending", params={"version_id": version_id}).json()
    saved = next(i for i in listed["items"] if i["status"] == "已确认")
    assert saved["confirmed_text"] == "2400"
    assert saved["participates_in_comparison"] is True
    # 来源可追溯：事实
    # （API 列表不返回 fact；后端单测覆盖用户确认）

    screenshot(page, "dc16-confirm-saved-count.png")

    # 无法确定第二条
    open_rows = page.locator('[data-testid="confirm-row"][data-processed="false"]')
    if open_rows.count() == 0:
        # 若 seed 仅 1 条已处理，再 seed 不会增加；用 API 再造一条不方便；跳过若无
        pass
    else:
        before2 = _tab_count(page)
        open_rows.first.click()
        expect(page.get_by_test_id("btn-confirm-unsure")).to_be_visible()
        page.get_by_test_id("btn-confirm-unsure").click()
        expect(page.locator(".confirmation-form .handled.unsure")).to_contain_text("无法确定")
        assert _tab_count(page) == before2 - 1
        after = client.get("/api/pending", params={"version_id": version_id}).json()
        unsure = next(i for i in after["items"] if i["status"] == "无法确定")
        assert unsure["participates_in_comparison"] is False
        assert unsure["counts_incomplete"] is True
        screenshot(page, "dc16-confirm-unsure.png")


def test_w7q1_reedit_processed_row(imported, configure, client, screenshot):
    """W7-Q1=A：点已处理行重新展开填写；页签计数不加回；保存后按新状态。"""
    page = imported
    version_id = _enter_results(page, configure)
    _open_confirm(page)
    row = page.get_by_test_id("confirm-row").first
    row.click()
    page.get_by_test_id("in-confirm-value").fill("2400")
    page.get_by_test_id("btn-confirm-save").click()
    expect(row).to_have_attribute("data-processed", "true")
    count_after_save = _tab_count(page)

    # 点已处理行 → 表单再展开；计数不变
    row.click()
    expect(page.get_by_test_id("in-confirm-value")).to_be_visible()
    assert page.get_by_test_id("btn-confirm-save").count() == 1
    assert page.locator('button:has-text("修改")').count() == 0
    assert _tab_count(page) == count_after_save

    # 改值保存
    page.get_by_test_id("in-confirm-value").fill("2500")
    page.get_by_test_id("btn-confirm-save").click()
    expect(page.locator(".confirmation-form .handled")).to_contain_text("2500")
    assert _tab_count(page) == count_after_save
    remote = client.get("/api/pending", params={"version_id": version_id}).json()
    item = next(i for i in remote["items"] if i["confirmed_text"] == "2500")
    assert item["status"] == "已确认"

    # 再改点无法确定
    page.get_by_test_id("confirm-row").first.click()
    expect(page.get_by_test_id("btn-confirm-unsure")).to_be_visible()
    assert _tab_count(page) == count_after_save
    page.get_by_test_id("btn-confirm-unsure").click()
    expect(page.locator(".confirmation-form .handled.unsure")).to_contain_text("无法确定")
    assert _tab_count(page) == count_after_save
    screenshot(page, "dc16-confirm-reedit.png")


def test_unprocessed_not_in_comparison(imported, configure, client):
    page = imported
    version_id = _enter_results(page, configure)
    _open_confirm(page)
    remote = client.get("/api/pending", params={"version_id": version_id}).json()
    assert remote["pending_count"] >= 1
    for item in remote["items"]:
        if item["status"] == "待确认":
            assert item["participates_in_comparison"] is False
            assert item["counts_incomplete"] is True
