from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.models.errors import CODE_AUTH_INVALID, CODE_BAD_RESPONSE, CODE_CERTIFICATE_ERROR, CODE_RATE_LIMITED
from backend.models.mock import MockModelClient
from backend.models.openai_compat import OpenAICompatClient, SecretStr
from backend.settings.codes import (
    ERR_LOCAL_URL_REQUIRED,
    ERR_MONTHLY_LIMIT_REQUIRED,
    ERR_PRESET_URL_LOCKED,
    ERR_PRIMARY_REQUIRED,
    ERR_SAME_MODEL,
    MESSAGES,
    MODEL_STATUS_DUAL,
    MODEL_STATUS_NONE,
    MODEL_STATUS_SINGLE,
    PRECHECK_COST_EXCEEDED,
    PRECHECK_COST_WARNING,
    PRECHECK_LIMIT_REQUIRED,
    PRECHECK_NO_MODEL,
    PRECHECK_OK,
    TEST_CREDENTIAL_REQUIRED,
    TEST_OK,
    cost_warning_message,
    format_yuan_amount,
)
from backend.settings.credentials import CredentialService, MemoryCredentialBackend, env_credential_allowed
from backend.settings.service import SettingsError, SettingsService, dual_model_guard
from backend.store import Repository


@pytest.fixture
def svc(tmp_path: Path):
    repo = Repository(tmp_path)
    creds = CredentialService(MemoryCredentialBackend(), allow_env=False)
    service = SettingsService(
        repo,
        credentials=creds,
        client_factory=lambda slot, secret: MockModelClient(
            branch=MockModelClient.BRANCH_SUCCESS,
            provider=slot.get("provider") or "mock",
            model=slot.get("model") or "mock-vl",
        ),
    )
    yield service
    repo.close()


def _base_save(svc: SettingsService, **extra):
    payload = {
        "monthly_limit_yuan": 100,
        "dimension_tolerance_mm": 1,
        "elevation_tolerance_m": 0.005,
        "primary_model": {
            "provider": "siliconflow",
            "model": "zai-org/GLM-4.5V",
            "base_url": "https://api.siliconflow.cn/v1",
            "credential_ref": "",
        },
    }
    payload.update(extra)
    return svc.save_settings(payload, primary_api_key="mock-key-primary")


def test_empty_key_test_connection(svc: SettingsService):
    svc.repo.save_settings({
        "monthly_limit_yuan": 10,
        "primary_model": {
            "provider": "siliconflow",
            "model": "zai-org/GLM-4.5V",
            "base_url": "https://api.siliconflow.cn/v1",
            "credential_ref": "",
        },
    })
    result = svc.test_connection(role="primary")
    assert result["code"] == TEST_CREDENTIAL_REQUIRED
    assert result["ok"] is False


def test_mock_test_connection_ok_and_invalidate(svc: SettingsService):
    _base_save(svc)
    ok = svc.test_connection(role="primary")
    assert ok["code"] == TEST_OK
    assert svc.is_test_result_valid("primary")

    # 改服务商 → 作废
    svc.save_settings({
        "monthly_limit_yuan": 100,
        "primary_model": {
            "provider": "qwen",
            "model": "qwen-vl-max",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "credential_ref": "",
        },
    }, primary_api_key="mock-key-2")
    assert svc.is_test_result_valid("primary") is False

    svc.test_connection(role="primary")
    assert svc.is_test_result_valid("primary")

    # 改型号
    view = svc.get_settings_view()
    slot = dict(view["primary_model"])
    slot["model"] = "qwen-vl-plus"
    svc.save_settings({"monthly_limit_yuan": 100, "primary_model": slot}, primary_api_key="mock-key-2")
    assert svc.is_test_result_valid("primary") is False

    svc.test_connection(role="primary")
    # 改密钥
    slot = dict(svc.get_settings_view()["primary_model"])
    svc.save_settings({"monthly_limit_yuan": 100, "primary_model": slot}, primary_api_key="mock-key-changed")
    assert svc.is_test_result_valid("primary") is False


def test_preset_url_locked_requires_custom(svc: SettingsService):
    """M8：改预设地址须改为自定义。"""
    with pytest.raises(SettingsError) as ei:
        svc.save_settings({
            "monthly_limit_yuan": 10,
            "primary_model": {
                "provider": "siliconflow",
                "model": "zai-org/GLM-4.5V",
                "base_url": "https://evil.example/v1",
                "credential_ref": "",
            },
        }, primary_api_key="mock-key")
    assert ei.value.code == ERR_PRESET_URL_LOCKED


def test_model_status_none_single_dual(svc: SettingsService, tmp_path: Path):
    assert svc.model_status() == MODEL_STATUS_NONE
    blocked = svc.precheck_start_review()
    assert blocked["code"] == PRECHECK_NO_MODEL
    assert blocked["task_id"] is None

    # 无上限也不能开始
    svc.repo.save_settings({
        "primary_model": {
            "provider": "siliconflow",
            "model": "zai-org/GLM-4.5V",
            "base_url": "https://api.siliconflow.cn/v1",
            "credential_ref": "env:SILICONFLOW_API_KEY",
        },
    })
    r = svc.precheck_start_review()
    assert r["code"] == PRECHECK_LIMIT_REQUIRED

    _base_save(svc)
    assert svc.model_status() == MODEL_STATUS_SINGLE
    project = svc.repo.create_project("t")
    version = svc.repo.create_version(project["id"], "v1")
    ok = svc.precheck_start_review(version_id=version["id"])
    assert ok["code"] == PRECHECK_OK
    assert ok["model_status"] == MODEL_STATUS_SINGLE
    assert ok["single_model_notice"]
    assert ok["task_id"]

    svc.save_settings({
        "monthly_limit_yuan": 100,
        "primary_model": {
            "provider": "siliconflow",
            "model": "zai-org/GLM-4.5V",
            "base_url": "https://api.siliconflow.cn/v1",
            "credential_ref": "",
        },
        "review_model": {
            "provider": "siliconflow",
            "model": "Qwen/Qwen3-VL-32B-Instruct",
            "base_url": "https://api.siliconflow.cn/v1",
            "credential_ref": "",
        },
    }, primary_api_key="mock-key-primary", review_api_key="mock-key-review")
    assert svc.model_status() == MODEL_STATUS_DUAL


def test_reject_same_model_and_review_only_message(svc: SettingsService):
    """M5：只配复核拦截文案逐字。"""
    with pytest.raises(SettingsError) as ei:
        svc.save_settings({
            "monthly_limit_yuan": 10,
            "review_model": {
                "provider": "siliconflow",
                "model": "Qwen/Qwen3-VL-32B-Instruct",
                "base_url": "https://api.siliconflow.cn/v1",
                "credential_ref": "",
            },
            "primary_model": None,
        }, review_api_key="mock-key")
    assert ei.value.code == ERR_PRIMARY_REQUIRED
    assert ei.value.message == MESSAGES[ERR_PRIMARY_REQUIRED]
    assert ei.value.message == "请先填主模型；只用一个模型时填在主模型里"

    with pytest.raises(SettingsError) as ei2:
        svc.save_settings({
            "monthly_limit_yuan": 10,
            "primary_model": {
                "provider": "siliconflow",
                "model": "Qwen/Qwen3-VL-32B-Instruct",
                "base_url": "https://api.siliconflow.cn/v1",
                "credential_ref": "",
            },
            "review_model": {
                "provider": "siliconflow",
                "model": "Qwen/Qwen3-VL-32B-Instruct",
                "base_url": "https://api.siliconflow.cn/v1",
                "credential_ref": "",
            },
        }, primary_api_key="mock-a", review_api_key="mock-b")
    assert ei2.value.code == ERR_SAME_MODEL

    with pytest.raises(SettingsError):
        dual_model_guard(
            {"provider": "siliconflow", "model": "Qwen/Qwen3-VL-32B-Instruct", "base_url": ""},
            {"provider": "siliconflow", "model": "Qwen/Qwen3-VL-32B-Instruct", "base_url": ""},
        )


def test_cross_provider_same_base_rejected_on_save(svc: SettingsService):
    """M5：跨服务商同底座经 save_settings 被拒。"""
    with pytest.raises(SettingsError) as ei:
        svc.save_settings({
            "monthly_limit_yuan": 10,
            "primary_model": {
                "provider": "zhipu",
                "model": "glm-4.5v",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "credential_ref": "",
            },
            "review_model": {
                "provider": "siliconflow",
                "model": "zai-org/GLM-4.5V",
                "base_url": "https://api.siliconflow.cn/v1",
                "credential_ref": "",
            },
        }, primary_api_key="mock-a", review_api_key="mock-b")
    assert ei.value.code == ERR_SAME_MODEL
    assert ei.value.message == MESSAGES[ERR_SAME_MODEL]


def test_no_same_model_dual_path_in_codebase():
    """静态检查：设置/模型层不存在「同一模型执行两次」的冒充复核路径。"""
    root = Path(__file__).resolve().parents[3]
    suspects = []
    for path in (root / "backend/models").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "run_twice" in text or "same_model_twice" in text:
            suspects.append(str(path))
    assert suspects == []
    src = (root / "backend/settings/service.py").read_text(encoding="utf-8")
    assert "dual_model_guard" in src
    assert "same_base_model" in src


def test_local_requires_url(svc: SettingsService):
    with pytest.raises(SettingsError) as ei:
        svc.save_settings({
            "monthly_limit_yuan": 10,
            "primary_model": {
                "provider": "local",
                "model": "local-vl",
                "base_url": "",
                "credential_ref": "",
            },
        })
    assert ei.value.code == ERR_LOCAL_URL_REQUIRED


def test_monthly_limit_required_on_save(svc: SettingsService):
    with pytest.raises(SettingsError) as ei:
        svc.save_settings({
            "primary_model": {
                "provider": "siliconflow",
                "model": "zai-org/GLM-4.5V",
                "base_url": "https://api.siliconflow.cn/v1",
                "credential_ref": "",
            },
        }, primary_api_key="mock-key")
    assert ei.value.code == ERR_MONTHLY_LIMIT_REQUIRED


def test_cost_warning_and_exceed(svc: SettingsService, monkeypatch):
    _base_save(svc, monthly_limit_yuan=1)
    svc.review_cost_reader = lambda now: 850_000
    project = svc.repo.create_project("c")
    version = svc.repo.create_version(project["id"], "v1")
    result = svc.precheck_start_review(version_id=version["id"])
    assert result["ok"] is True
    assert result["warning_code"] == PRECHECK_COST_WARNING
    assert "80%" in result["warning_message"]
    assert "e-" not in result["warning_message"]  # M10 不用科学计数法
    assert "约" in result["warning_message"]

    result2 = svc.precheck_start_review(version_id=version["id"])
    assert result2.get("warning_code") is None

    svc.review_cost_reader = lambda now: 1_000_000
    blocked = svc.precheck_start_review(version_id=version["id"])
    assert blocked["ok"] is False
    assert blocked["code"] == PRECHECK_COST_EXCEEDED
    assert blocked["task_id"] is None
    assert "审查已暂停" in blocked["message"]

    svc.save_settings({"monthly_limit_yuan": 2})
    svc.review_cost_reader = lambda now: 1_600_000
    again = svc.precheck_start_review(version_id=version["id"])
    assert again["ok"] is True
    assert again.get("warning_code") == PRECHECK_COST_WARNING


def test_test_connection_cost_counts_and_allowed_when_exceeded(svc: SettingsService):
    """M5：测试连接费用计入本月；超限后仍可测试连接。"""
    _base_save(svc, monthly_limit_yuan=0.000001)  # 1 微元上限，几乎立刻超限
    before = svc.ledger.month_cost_micros(now=svc.clock())
    ok = svc.test_connection(role="primary")
    assert ok["code"] == TEST_OK
    after = svc.ledger.month_cost_micros(now=svc.clock())
    assert after > before

    # 超限后审查阻断，但仍可测试连接
    project = svc.repo.create_project("x")
    version = svc.repo.create_version(project["id"], "v1")
    # 强制费用已超
    svc.review_cost_reader = lambda now: 10_000_000
    blocked = svc.precheck_start_review(version_id=version["id"])
    assert blocked["code"] == PRECHECK_COST_EXCEEDED
    still = svc.test_connection(role="primary")
    assert still["code"] == TEST_OK


def test_local_timezone_month_boundary(svc: SettingsService):
    """M2/M5：本机时区自然月边界（UTC 月末 16:00 属 +08 下月）。"""
    # 注入 America/Los_Angeles（UTC-8）时钟
    la = ZoneInfo("America/Los_Angeles")
    # 2026-02-01 00:30 LA = 仍是 2 月
    svc.clock = lambda: datetime(2026, 2, 1, 0, 30, tzinfo=la)
    _base_save(svc, monthly_limit_yuan=10)
    # 台账一条：UTC 2026-02-01 07:00 = LA 2026-01-31 23:00 → 属 1 月，不算入 2 月
    svc.ledger.add({
        "time": "2026-02-01T07:00:00+00:00",
        "model": "m",
        "role": "primary",
        "provider": "siliconflow",
        "estimated_cost_micros": 999,
        "status": "ok",
        "code": "CONNECTION_OK",
        "image": False,
    })
    # 另一条：UTC 2026-02-01 09:00 = LA 2026-02-01 01:00 → 属 2 月
    svc.ledger.add({
        "time": "2026-02-01T09:00:00+00:00",
        "model": "m",
        "role": "primary",
        "provider": "siliconflow",
        "estimated_cost_micros": 111,
        "status": "ok",
        "code": "CONNECTION_OK",
        "image": False,
    })
    assert svc.ledger.month_cost_micros(now=svc.clock()) == 111


def test_tolerance_defaults_and_update(svc: SettingsService):
    s = svc.repo.get_settings()
    assert s["dimension_tolerance_mm"] == 1
    assert s["elevation_tolerance_m"] == 0.005
    _base_save(svc, dimension_tolerance_mm=2, elevation_tolerance_m=0.01)
    s2 = svc.repo.get_settings()
    assert s2["dimension_tolerance_mm"] == 2
    assert s2["elevation_tolerance_m"] == 0.01
    with pytest.raises(Exception):
        svc.repo.save_settings({"dimension_tolerance_mm": -1})


def test_meta_has_no_secret_sha(svc: SettingsService, tmp_path: Path):
    """M9：meta 不存无盐密钥哈希。"""
    _base_save(svc)
    svc.test_connection(role="primary")
    meta_text = (tmp_path / "dc08_settings_meta.json").read_text(encoding="utf-8")
    assert "secret_sha" not in meta_text
    assert "credential_versions" in meta_text or "fingerprint" in meta_text


def test_product_mode_disallows_env_by_default(monkeypatch):
    """M8：产品模式默认不读环境变量密钥。"""
    monkeypatch.delenv("DRAWCHECK_DEV", raising=False)
    monkeypatch.delenv("DRAWCHECK_LIVE_MODEL", raising=False)
    assert env_credential_allowed() is False
    creds = CredentialService(MemoryCredentialBackend())
    assert creds.allow_env is False
    assert creds.resolve("env:SILICONFLOW_API_KEY").get() == ""


def _assert_canary_absent(canary: str, *parts: str, paths: list[Path], caplog, capsys):
    blob = "".join(parts) + capsys.readouterr().out + capsys.readouterr().err
    for rec in caplog.records:
        blob += rec.getMessage() + repr(rec.msg)
    assert canary not in blob
    for path in paths:
        if path.is_file():
            assert canary.encode() not in path.read_bytes()
        elif path.is_dir():
            for f in path.rglob("*"):
                if f.is_file():
                    assert canary.encode() not in f.read_bytes()


@pytest.mark.parametrize(
    "status,expect_code",
    [
        (401, CODE_AUTH_INVALID),
        (429, CODE_RATE_LIMITED),
        (400, "OTHER_HTTP_400"),
    ],
)
def test_canary_adapter_http_paths(svc: SettingsService, tmp_path: Path, capsys, caplog, status, expect_code):
    """M4：canary 走 401/429/格式等路径，返回与日志 0 出现。"""
    canary = f"canary-SF-HTTP-{status}-9f3a2c1b0e7d65432100aabbccddeeff"
    _base_save(svc)
    slot = dict(svc.get_settings_view()["primary_model"])
    svc.save_settings({"monthly_limit_yuan": 100, "primary_model": slot}, primary_api_key=canary)

    def handler(request: httpx.Request) -> httpx.Response:
        if status == 400:
            return httpx.Response(400, text="bad")
        return httpx.Response(status, json={"error": {"message": "x"}})

    def factory(slot, secret):
        return OpenAICompatClient(
            provider="siliconflow",
            base_url="https://api.siliconflow.cn/v1",
            api_key=secret,
            transport=httpx.MockTransport(handler),
            max_retries=0 if status != 429 else 1,
            retry_backoff_sec=0,
        )

    svc.client_factory = factory
    with caplog.at_level(logging.DEBUG):
        result = svc.test_connection(role="primary")
    assert result["ok"] is False
    assert result["code"] == expect_code
    assert canary not in str(result) and canary not in repr(result)
    _assert_canary_absent(canary, str(result), paths=[tmp_path], caplog=caplog, capsys=capsys)


def test_canary_certificate_and_bad_format(svc: SettingsService, tmp_path: Path, capsys, caplog):
    canary = "canary-SF-CERT-FMT-9f3a2c1b0e7d65432100aabbccddeeff"
    _base_save(svc)
    slot = dict(svc.get_settings_view()["primary_model"])
    svc.save_settings({"monthly_limit_yuan": 100, "primary_model": slot}, primary_api_key=canary)

    # 证书错误经 Mock 分支
    svc.client_factory = lambda slot, secret: MockModelClient(branch=MockModelClient.BRANCH_CERT)
    with caplog.at_level(logging.DEBUG):
        r1 = svc.test_connection(role="primary")
    assert r1["code"] == CODE_CERTIFICATE_ERROR

    svc.client_factory = lambda slot, secret: MockModelClient(branch=MockModelClient.BRANCH_BAD_FORMAT)
    with caplog.at_level(logging.DEBUG):
        r2 = svc.test_connection(role="primary")
    assert r2["code"] == CODE_BAD_RESPONSE
    _assert_canary_absent(canary, str(r1), str(r2), paths=[tmp_path], caplog=caplog, capsys=capsys)


def test_canary_via_rest_test_connection(tmp_path: Path, monkeypatch, capsys, caplog):
    """M4：REST 返回中 canary 0 出现。"""
    canary = "canary-SF-REST-9f3a2c1b0e7d65432100aabbccddeeff"
    monkeypatch.setenv("DRAWCHECK_DATA_DIR", str(tmp_path))
    app = create_app(token="test-token-dc08")
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token-dc08"}
    r = client.get("/api/health", headers=headers)
    assert r.status_code == 200
    # 未配置模型时测试连接应返回错误码；响应不得含 canary
    bad = client.post(
        "/api/settings/test-connection",
        json={"role": "primary", "use_image": False},
        headers=headers,
    )
    body = bad.text
    assert canary not in body
    assert canary not in repr(bad.json())


def test_live_marker_skipped_without_flag():
    import tests.backend.models.test_live_siliconflow as live_mod
    assert hasattr(live_mod, "LIVE")


def test_format_amount_helper():
    """M10/N16：settings 侧金额助手与 80% 文案。"""
    assert format_yuan_amount(0) == "0.00 元"
    assert format_yuan_amount(1.2e-4) == "不足 0.01 元"
    assert "e" not in format_yuan_amount(1.2e-4).lower()
    assert format_yuan_amount(40, kind="estimate") == "约 40.00 元"
    assert format_yuan_amount(3.25, kind="estimate") == "约 3.25 元"
    assert format_yuan_amount(0, kind="estimate") == "0.00 元"
    assert format_yuan_amount(-1.0, kind="estimate") == "0.00 元"
    assert "约" not in format_yuan_amount(0, kind="estimate")
    assert format_yuan_amount(50, kind="limit") == "50.00 元"
    assert cost_warning_message(40, 50) == (
        "本月模型费用估算已用 80%（约 40.00 元 / 上限 50.00 元）"
    )
    assert cost_warning_message(0, 50) == (
        "本月模型费用估算已用 80%（0.00 元 / 上限 50.00 元）"
    )


def test_monthly_limit_input_not_rewritten_by_formatter(svc: SettingsService):
    """M10：上限输入框保持用户原样；format 函数只产展示文案，不回写原值。"""
    # 用户输入 50（整数）与 50.5，存库应保持原样数值，而非「50.00 元」
    _base_save(svc, monthly_limit_yuan=50)
    assert svc.repo.get_settings()["monthly_limit_yuan"] == 50
    svc.save_settings({"monthly_limit_yuan": 50.5})
    assert svc.repo.get_settings()["monthly_limit_yuan"] == 50.5
    # 展示函数不改变调用方传入的 limit 字段语义
    assert format_yuan_amount(50, kind="limit") == "50.00 元"
    assert format_yuan_amount(50.5, kind="limit") == "50.50 元"


def test_format_yuan_negative_logs(caplog):
    """M10：负数按 0.00 元显示并记日志。"""
    import logging
    with caplog.at_level(logging.WARNING, logger="backend.settings.codes"):
        assert format_yuan_amount(-3.14) == "0.00 元"
    assert any("负数" in r.message for r in caplog.records)
