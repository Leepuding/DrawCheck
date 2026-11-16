"""A11/A12/A13：密度、弱入口、溢出及保密。"""
import re
from playwright.sync_api import expect


def assert_global(page, settings=False):
    text = page.locator("body").text_content()
    assert "原型演示" not in text and "重置原型" not in text
    assert not re.search(r"localhost|127\.0\.0\.1|__launch\?t=|Bearer\s|端口|令牌", text, re.I)
    if not settings: assert text.count("容差") == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # A12：不含行内复核、对话框内按钮
    assert page.locator("button:visible").evaluate_all(
        "els => els.filter(e => !e.closest('.review-actions') && !e.closest('dialog')).length"
    ) <= 8
    for name in ["btn-import", "btn-review", "btn-export"]:
        rect = page.get_by_test_id(name).bounding_box()
        assert rect and rect["y"] >= 0 and rect["y"] + rect["height"] <= 800
    assert page.get_by_test_id("btn-settings").bounding_box()["width"] < page.get_by_test_id("btn-import").bounding_box()["width"]


def test_all_states_and_gear_during_progress(imported, configure):
    page = imported
    assert_global(page)
    configure(dual=True)
    page.get_by_test_id("btn-settings").click()
    expect(page.get_by_test_id("page-settings")).to_be_visible()
    assert_global(page, settings=True)
    page.get_by_test_id("btn-settings-back").click()
    page.get_by_test_id("btn-review").click()
    expect(page.get_by_test_id("dlg-disclosure")).to_be_visible()
    assert_global(page)
    page.get_by_test_id("btn-disclosure-ok").click()
    expect(page.get_by_test_id("progress")).to_be_visible()
    assert_global(page)
    page.get_by_test_id("btn-settings").click()
    expect(page.get_by_test_id("page-settings")).to_be_visible()
    assert_global(page, settings=True)
    # 进度期间设置返回不会丢失进度，结束后返回结果壳。
    page.wait_for_timeout(4000)
    page.get_by_test_id("btn-settings-back").click()
    expect(page.locator("body")).to_have_attribute("data-state", "results")
    assert_global(page)
    page.get_by_test_id("btn-settings").click()
    expect(page.get_by_test_id("page-settings")).to_be_visible()
    page.get_by_test_id("btn-settings-save").click()
    expect(page.locator("body")).to_have_attribute("data-state", "results")


def test_idle_mobile_reflow(page, screenshot):
    assert_global(page)
    page.set_viewport_size({"width":390,"height":844})
    assert_global(page)
    screenshot(page, "08-mobile-idle.png")
    page.get_by_test_id("btn-settings").click()
    expect(page.get_by_test_id("page-settings")).to_be_visible()
    assert_global(page, settings=True)
    screenshot(page, "09-mobile-settings.png")
