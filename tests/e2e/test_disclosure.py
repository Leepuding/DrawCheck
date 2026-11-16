"""A3/A5：前置阻断、裁定文案、取消、进度与占位边界。"""
import pytest
from playwright.sync_api import expect


def test_no_model_blocks_before_disclosure(imported, client, screenshot):
    page = imported
    with page.expect_response("**/api/settings/precheck") as response:
        page.get_by_test_id("btn-review").click()
    assert response.value.json()["code"] == "NO_MODEL"
    assert response.value.json()["task_id"] is None
    expect(page.get_by_test_id("toast-block")).to_have_text("先配置模型才能审查")
    expect(page.get_by_test_id("page-settings")).to_be_visible()
    expect(page.get_by_test_id("in-model-provider")).to_be_focused()
    expect(page.get_by_test_id("dlg-disclosure")).to_have_count(0)
    screenshot(page, "04-block.png")
    page.get_by_test_id("in-model-key-primary").fill("test-dc14-primary")
    page.get_by_test_id("in-fee-cap").fill("200")
    page.get_by_test_id("btn-settings-save").click()
    expect(page.locator("body")).to_have_attribute("data-state", "imported")


@pytest.mark.parametrize("dual", [False, True])
def test_disclosure_progress_and_results(imported, configure, dual, screenshot, client):
    page = imported
    configure(dual)
    page.get_by_test_id("btn-review").click()
    dialog = page.get_by_test_id("dlg-disclosure")
    expect(dialog).to_be_visible()
    providers = "硅基流动、通义千问" if dual else "硅基流动"
    expect(dialog.locator(".disclosure-copy")).to_have_text(f"整张图会发给你配置的模型（{providers}），用于找出图纸之间的不一致。")
    note = "范围、相关图和双模型复核会自动完成" if dual else "范围、相关图会自动完成"
    expect(dialog.locator(".disclosure-note").first).to_have_text(note)
    assert ("只配了一个模型，本次结果未经第二个模型复核。" in dialog.inner_text()) is not dual
    assert "大幅图纸会分区域识别，可能增加用时和模型费用。" in dialog.inner_text()
    assert dialog.locator("input").count() == 0
    assert "容差" not in page.locator("body").text_content()
    screenshot(page, f"05-disclosure-{'dual' if dual else 'single'}.png")
    page.get_by_test_id("btn-disclosure-cancel").click()
    expect(dialog).to_have_count(0)
    expect(page.locator("body")).to_have_attribute("data-state", "imported")
    page.get_by_test_id("btn-review").click()
    page.get_by_test_id("btn-disclosure-ok").click()
    expect(page.get_by_test_id("progress")).to_be_visible()
    expect(page.get_by_test_id("progress-title")).not_to_have_text("")
    screenshot(page, "06-progress.png")
    expect(page.locator("body")).to_have_attribute("data-state", "results", timeout=15000)
    expect(page.get_by_test_id("btn-export")).to_be_enabled()
    # UI follows actual persisted task status, not whether a second slot was configured.
    history = client.get('/api/history').json()['items'][0]
    opened = client.post('/api/history/open', json={'project_id': history['project_id']}).json()
    progress = client.get(f"/api/tasks/{opened['task_id']}").json()
    expect(page.get_by_test_id("results-title")).to_have_text("审查未完成" if progress['status'] in ('失败', '暂停') else "审查部分完成" if progress['outcome'] == 'partial' else "审查完成")
    if progress['incomplete_count']:
        panel = page.get_by_test_id("incomplete-panel")
        expect(panel.locator("summary")).to_have_text("未核验")
        if panel.get_attribute("open") is None:
            panel.locator("summary").click()
        expect(panel.get_by_role("heading", name="识别或读取未完成")).to_be_visible()
    assert "审查完成（占位）" not in page.locator("body").inner_text()
    assert "没问题" not in page.locator("body").inner_text()
    assert "审查通过" not in page.locator("body").inner_text()
    screenshot(page, "07-results.png")


@pytest.mark.parametrize("code,expected", [
    ("COST_WARNING", "本月模型费用估算已用 80%（约 160.00 元 / 上限 200.00 元）"),
    ("COST_EXCEEDED", "本月费用估算已到上限，审查已暂停。可在设置里调高上限后点「开始审查」继续。"),
    ("LIMIT_REQUIRED", "请填写每月费用上限（元）"),
])
def test_cost_precheck_not_ignored(imported, configure, code, expected):
    page = imported
    configure()
    result = {"ok":code == "COST_WARNING", "code":"OK" if code == "COST_WARNING" else code}
    if result["ok"]: result.update(warning_code=code, warning_message=expected,
                                   disclosure_id="test-disclosure", model_status="single", providers=["硅基流动"])
    page.route("**/api/settings/precheck", lambda route: route.fulfill(json=result))
    page.get_by_test_id("btn-review").click()
    expect(page.get_by_test_id("toast-block")).to_have_text(expected)
    if result["ok"]: expect(page.get_by_test_id("dlg-disclosure")).to_be_visible()
    else:
        expect(page.get_by_test_id("page-settings")).to_be_visible()
        expect(page.get_by_test_id("dlg-disclosure")).to_have_count(0)


def test_model_change_in_another_session_requires_new_disclosure(imported, configure):
    page = imported
    configure()
    page.get_by_test_id("btn-review").click()
    dialog = page.get_by_test_id("dlg-disclosure")
    expect(dialog.locator(".disclosure-copy")).to_contain_text("硅基流动）")
    configure(dual=True)
    page.get_by_test_id("btn-disclosure-ok").click()
    expect(page.get_by_test_id("toast-block")).to_have_text("请重新点「开始审查」并确认模型告知。")
    expect(page.locator("body")).to_have_attribute("data-state", "imported")
    page.get_by_test_id("btn-review").click()
    expect(dialog.locator(".disclosure-copy")).to_contain_text("硅基流动、通义千问")
