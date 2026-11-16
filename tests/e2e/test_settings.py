"""A2、费用必填、连接文案与真实保存。"""
import pytest
from playwright.sync_api import expect


def open_settings(page):
    page.get_by_test_id("btn-settings").click()
    expect(page.get_by_test_id("page-settings")).to_be_visible()


def test_three_sections_empty_guards_and_persistence(page, client, screenshot):
    open_settings(page)
    assert page.locator("fieldset legend").all_text_contents() == ["模型", "费用上限", "容差"]
    assert page.get_by_test_id("in-model-provider").locator("option").first.inner_text() == "硅基流动"
    expect(page.get_by_test_id("in-tolerance")).to_have_value("1")
    expect(page.get_by_test_id("in-tolerance-elevation")).to_have_value("0.005")
    expect(page.get_by_test_id("in-model-key-primary")).to_have_attribute("type", "password")
    requests = []
    page.on("request", lambda request: requests.append(request.url) if "test-connection" in request.url else None)
    page.get_by_test_id("btn-test-model-primary").click()
    expect(page.get_by_test_id("test-result-primary")).to_have_text("请先填写模型密钥")
    page.get_by_test_id("in-model-provider").select_option("local")
    page.get_by_test_id("in-model-key-primary").fill("test-dc14-local")
    page.get_by_test_id("btn-test-model-primary").click()
    expect(page.get_by_test_id("test-result-primary")).to_have_text("请先填写本地模型地址")
    assert requests == []
    page.get_by_test_id("in-model-provider").select_option("siliconflow")
    page.get_by_test_id("btn-settings-save").click()
    expect(page.get_by_test_id("settings-message")).to_have_text("请填写每月费用上限（元）")
    page.get_by_test_id("in-model-key-primary").fill("test-dc14-primary")
    page.get_by_test_id("in-fee-cap").fill("1234.56")
    page.get_by_test_id("in-tolerance").fill("2")
    page.get_by_test_id("in-tolerance-elevation").fill("0.01")
    page.get_by_test_id("btn-test-model-primary").click()
    expect(page.get_by_test_id("test-result-primary")).to_have_text("连接正常")
    expect(page.get_by_test_id("settings-message")).to_be_empty()
    expect(page.get_by_test_id("in-model-key-primary")).to_have_value("")
    assert "主模型：" in page.get_by_test_id("model-display").inner_text()
    screenshot(page, "02-settings.png")
    page.get_by_test_id("btn-settings-save").click()
    expect(page.locator("body")).to_have_attribute("data-state", "idle")
    page.reload(wait_until="networkidle")
    assert "还没配置模型" not in page.locator("body").inner_text()
    open_settings(page)
    expect(page.get_by_test_id("in-fee-cap")).to_have_value("1234.56")
    expect(page.get_by_test_id("in-tolerance")).to_have_value("2")
    assert client.get("/api/settings").json()["monthly_limit_yuan"] == 1234.56
    assert "test-dc14-primary" not in client.get("/api/settings").text
    assert page.evaluate("localStorage.length") == 0


def test_review_only_blocked_and_clear_review(page, client, configure):
    open_settings(page)
    page.get_by_test_id("in-fee-cap").fill("200")
    page.get_by_test_id("in-model-key-review").fill("test-dc14-review")
    page.get_by_test_id("btn-settings-save").click()
    expect(page.get_by_test_id("settings-message")).to_have_text("请先填主模型；只用一个模型时填在主模型里")
    assert client.get("/api/settings").json()["model_status"] == "none"
    page.get_by_test_id("btn-settings-back").click()
    configure(dual=True)
    open_settings(page)
    page.locator('[data-action="clear-review"]').click()
    page.get_by_test_id("btn-settings-save").click()
    expect(page.locator("body")).to_have_attribute("data-state", "idle")
    assert client.get("/api/settings").json()["model_status"] == "single"


@pytest.mark.parametrize("code,expected", [
    ("CREDENTIAL_REQUIRED", "请先填写模型密钥"),
    ("LOCAL_URL_REQUIRED", "请先填写本地模型地址"),
    ("AUTH_INVALID", "密钥无效或没有权限，请检查后重新填写"),
    ("BALANCE_INSUFFICIENT", "服务商账户余额不足，请到服务商处充值"),
    ("RATE_LIMITED", "服务商暂时限制了调用次数，请稍后再试"),
    ("TIMEOUT", "连接超时，请检查网络后再试"),
    ("CERTIFICATE_ERROR", "无法建立安全连接，请检查网络或代理设置"),
    ("VISION_UNSUPPORTED", "这个模型不能看图，请换一个支持看图的模型"),
    ("LOCAL_UNREACHABLE", "连不上本地模型地址，请确认地址正确且本地模型已启动"),
    ("OTHER", "连接失败（错误码：OTHER），请稍后再试"),
    ("OTHER_NET_ConnectError", "连接失败（错误码：OTHER_NET_ConnectError），请稍后再试"),
])
def test_failure_mapping(page, configure, code, expected):
    configure()
    open_settings(page)
    # 显式替换失败响应，验证前端映射；保存请求仍走真实后端。
    page.route("**/api/settings/test-connection", lambda route: route.fulfill(json={"ok":False,"code":code,"message":"需先填写"}))
    page.get_by_test_id("btn-test-model-primary").click()
    expect(page.get_by_test_id("test-result-primary")).to_have_text(expected)
    assert "需先填写" not in page.locator("body").inner_text()


def test_provider_switch_does_not_reuse_secret(page, client, configure):
    configure()
    open_settings(page)
    page.get_by_test_id("in-model-provider").select_option("qwen")
    page.get_by_test_id("btn-test-model-primary").click()
    expect(page.get_by_test_id("test-result-primary")).to_have_text("请先填写模型密钥")
    page.get_by_test_id("btn-settings-save").click()
    expect(page.locator("body")).to_have_attribute("data-state", "idle")
    assert client.get("/api/settings").json()["model_status"] == "none"
