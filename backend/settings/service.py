"""设置服务：存取、就绪、前置检查、测试连接。"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from backend.models.cost import MICROS_PER_YUAN, estimate_cost_micros
from backend.models.errors import (
    CODE_CREDENTIAL_REQUIRED,
    CODE_OK,
    ModelError,
)
from backend.models.mock import MockModelClient
from backend.models.openai_compat import OpenAICompatClient, SecretStr
from backend.models.presets import (
    get_preset,
    is_preset_url_allowed,
    list_presets,
    normalize_base_url,
    price_for_model,
    same_base_model,
)
from backend.settings.codes import (
    ERR_INVALID_SETTINGS,
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
    other_failure_message,
)
from backend.settings.credentials import CredentialError, CredentialService
from backend.settings.ledger import CallLedger
from backend.store import Repository
from backend.store.errors import StoreError


def _now_local() -> datetime:
    """本机时区当前时间（M2，裁定第 9 条）。"""
    return datetime.now().astimezone()


class SettingsError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or MESSAGES.get(code, "")
        super().__init__(self.message or code)

    def as_error(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


@dataclass
class TestConnectionState:
    fingerprint: str | None = None
    result_code: str | None = None
    result_message: str | None = None
    tested_at: str | None = None


def _tiny_png_data_url() -> str:
    """极小自制灰图（N1：use_image=true 时自动带图）。"""
    try:
        from PIL import Image
    except ImportError:
        # 无 Pillow 时退回 1×1 PNG 常量
        return (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
    img = Image.new("RGB", (32, 32), color=(180, 180, 180))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


class SettingsService:
    """围绕 Repository.get/save_settings 的业务层；不改 store 结构。"""

    def __init__(
        self,
        repo: Repository,
        *,
        credentials: CredentialService | None = None,
        ledger: CallLedger | None = None,
        client_factory: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        review_cost_reader: Callable[[datetime], int] | None = None,
    ):
        self.repo = repo
        self.credentials = credentials or CredentialService()
        data_root = Path(repo.root)
        self.ledger = ledger or CallLedger(data_root / "dc08_test_ledger.json")
        self._meta_path = data_root / "dc08_settings_meta.json"
        self._meta = self._load_meta()
        self.client_factory = client_factory
        self.clock = clock or _now_local
        # 审查调用费用读取器：默认只统计本单台账；后续 DC-10 可注入含 model_calls 的汇总
        self.review_cost_reader = review_cost_reader or self._default_cost_reader

    # ---------- 读取 ----------
    def get_settings_view(self) -> dict[str, Any]:
        settings = self.repo.get_settings()
        primary = settings.get("primary_model")
        review = settings.get("review_model")
        status = self.model_status(settings)
        primary_disp = self._slot_display(primary, "primary")
        review_disp = self._slot_display(review, "review")
        if status == MODEL_STATUS_DUAL and primary_disp and review_disp:
            model_display = f"主模型：{primary_disp}／复核模型：{review_disp}"
        elif primary_disp:
            model_display = f"主模型：{primary_disp}"
        elif review_disp:
            model_display = f"复核模型：{review_disp}"
        else:
            model_display = None
        # N13：历史脏数据只有复核时，告知引擎用复核顶主
        return {
            **settings,
            "model_status": status,
            "presets": list_presets(),
            "primary_display": primary_disp,
            "review_display": review_disp,
            "model_display": model_display,
            "effective_primary_role": self.effective_primary_role(settings),
            "test_connection": {
                "primary": self._meta.get("test_primary"),
                "review": self._meta.get("test_review"),
            },
            "credential_backend": self.credentials.backend_name,
        }

    def _slot_display(self, slot: dict | None, role: str) -> str | None:
        if not slot:
            return None
        model = slot.get("model") or ""
        return model or None

    # ---------- 保存 ----------
    def save_settings(
        self,
        changes: dict[str, Any],
        *,
        primary_api_key: str | None = None,
        review_api_key: str | None = None,
    ) -> dict[str, Any]:
        """增量保存。密钥经凭据服务，不入库。"""
        current = self.repo.get_settings()
        # N6：未知顶层字段拒绝
        known_keys = set(current.keys()) | {
            "dimension_tolerance_mm",
            "elevation_tolerance_m",
            "monthly_limit_yuan",
            "billing_period",
            "currency",
            "cost_warning_ratio",
            "primary_model",
            "review_model",
        }
        unknown = [k for k in changes.keys() if k not in known_keys]
        if unknown:
            raise SettingsError(ERR_INVALID_SETTINGS, f"未知字段: {', '.join(unknown)}")

        merged = {**current, **{k: v for k, v in changes.items() if k in known_keys}}

        # 费用上限必填（裁定第 7 条）
        limit = merged.get("monthly_limit_yuan")
        if limit is None:
            raise SettingsError(ERR_MONTHLY_LIMIT_REQUIRED, MESSAGES[ERR_MONTHLY_LIMIT_REQUIRED])

        primary = merged.get("primary_model")
        review = merged.get("review_model")

        if review and not primary:
            raise SettingsError(ERR_PRIMARY_REQUIRED, MESSAGES[ERR_PRIMARY_REQUIRED])

        primary = self._normalize_slot(primary, role="primary", api_key=primary_api_key)
        review = self._normalize_slot(review, role="review", api_key=review_api_key)
        merged["primary_model"] = primary
        merged["review_model"] = review

        if primary and review and same_base_model(
            primary["provider"], primary["model"], primary.get("base_url"),
            review["provider"], review["model"], review.get("base_url"),
        ):
            raise SettingsError(ERR_SAME_MODEL, MESSAGES[ERR_SAME_MODEL])

        try:
            saved = self.repo.save_settings({
                "dimension_tolerance_mm": merged["dimension_tolerance_mm"],
                "elevation_tolerance_m": merged["elevation_tolerance_m"],
                "monthly_limit_yuan": merged["monthly_limit_yuan"],
                "billing_period": merged.get("billing_period", "calendar_month"),
                "currency": merged.get("currency", "CNY"),
                "cost_warning_ratio": merged.get("cost_warning_ratio", 0.8),
                "primary_model": primary,
                "review_model": review,
            })
        except StoreError as exc:
            raise SettingsError(ERR_INVALID_SETTINGS, str(exc)) from exc

        # 配置指纹变化 → 作废测试结果
        self._invalidate_tests_if_changed(current, saved, primary_api_key, review_api_key)
        return self.get_settings_view()

    def _normalize_slot(self, slot: dict | None, *, role: str, api_key: str | None) -> dict | None:
        if slot is None:
            return None
        if not isinstance(slot, dict):
            raise SettingsError(ERR_INVALID_SETTINGS, "模型配置无效")
        # N6：槽位内未知字段（含误传 api_key）拒绝
        allowed_slot_keys = {"provider", "model", "base_url", "credential_ref"}
        unknown_slot = [k for k in slot.keys() if k not in allowed_slot_keys]
        if unknown_slot:
            raise SettingsError(ERR_INVALID_SETTINGS, f"模型配置含未知字段: {', '.join(unknown_slot)}")

        provider = slot.get("provider") or ""
        preset = get_preset(provider)
        if preset is None:
            raise SettingsError(ERR_INVALID_SETTINGS, f"未知服务商: {provider}")
        model = slot.get("model") or (
            preset.default_primary_model if role == "primary" else preset.default_review_model
        )
        if provider == "custom" and not model:
            raise SettingsError(ERR_INVALID_SETTINGS, "自定义服务商须填写型号")

        requested_url = slot.get("base_url")
        if requested_url is None or requested_url == "":
            base_url = preset.base_url
        else:
            base_url = requested_url

        # M8：预设服务商改地址须改为「自定义」并重新填密钥
        if preset.locks_base_url:
            if requested_url not in (None, "") and normalize_base_url(requested_url) != normalize_base_url(
                preset.base_url
            ):
                raise SettingsError(ERR_PRESET_URL_LOCKED, MESSAGES[ERR_PRESET_URL_LOCKED])
            if not is_preset_url_allowed(provider, base_url):
                raise SettingsError(ERR_PRESET_URL_LOCKED, MESSAGES[ERR_PRESET_URL_LOCKED])
            base_url = preset.base_url

        if preset.requires_base_url and not normalize_base_url(base_url):
            raise SettingsError(ERR_LOCAL_URL_REQUIRED, MESSAGES[ERR_LOCAL_URL_REQUIRED])

        credential_ref = slot.get("credential_ref") or ""
        if api_key is not None:
            if api_key == "":
                credential_ref = ""
            elif api_key.startswith("env:"):
                credential_ref = api_key
            else:
                from backend.settings.credentials import MemoryCredentialBackend

                # Keep earlier task references stable when a key is replaced.
                ref = f"model-{role}-{uuid.uuid4().hex}"
                allow_real = not isinstance(self.credentials.backend, MemoryCredentialBackend)
                # 开发：假钥可存 memory；真密钥请用 env:SILICONFLOW_API_KEY，禁止落入替身
                if not allow_real and not api_key.startswith(("mock-", "test-", "fake-", "canary-")):
                    raise SettingsError(
                        ERR_INVALID_SETTINGS,
                        "当前凭据后端为内存替身，真密钥请使用 credential_ref=env:SILICONFLOW_API_KEY",
                    )
                credential_ref = self.credentials.save_secret(ref, api_key, allow_real=allow_real)
                # M9：密钥版本号 +1（不存无盐哈希）
                self._bump_credential_version(credential_ref)

        return {
            "provider": provider,
            "model": model,
            "base_url": base_url or "",
            "credential_ref": credential_ref,
        }

    def _bump_credential_version(self, credential_ref: str) -> None:
        if not credential_ref:
            return
        versions = self._meta.setdefault("credential_versions", {})
        versions[credential_ref] = int(versions.get(credential_ref) or 0) + 1
        self._save_meta()

    # ---------- 就绪 / 前置检查 ----------
    def model_status(self, settings: dict | None = None) -> str:
        settings = settings or self.repo.get_settings()
        primary_ok = self._slot_configured(settings.get("primary_model"))
        review_ok = self._slot_configured(settings.get("review_model"))
        # 只配复核已在保存时拦；此处若历史脏数据只有复核，按单模型处理（用复核顶主）
        n = int(primary_ok) + int(review_ok)
        if n == 0:
            return MODEL_STATUS_NONE
        if n == 1:
            return MODEL_STATUS_SINGLE
        return MODEL_STATUS_DUAL

    def _slot_configured(self, slot: dict | None) -> bool:
        if not slot:
            return False
        provider = slot.get("provider") or ""
        preset = get_preset(provider)
        if preset is None:
            return False
        if preset.requires_base_url and not normalize_base_url(slot.get("base_url")):
            return False
        ref = slot.get("credential_ref") or ""
        if preset.requires_base_url:
            # 本地/自定义：地址齐全即算（密钥可选）
            return True
        return bool(ref)

    def effective_primary_role(self, settings: dict) -> str:
        if not self._slot_configured(settings.get("primary_model")) and self._slot_configured(settings.get("review_model")):
            return "review"
        return "primary"

    def precheck_start_review(self, *, version_id: str | None = None, settings: dict | None = None) -> dict[str, Any]:
        """开始审查前置检查。阻断时不建任务。"""
        if settings is None:
            settings = self.repo.get_settings()
        status = self.model_status(settings)
        if status == MODEL_STATUS_NONE:
            return {
                "ok": False,
                "code": PRECHECK_NO_MODEL,
                "message": MESSAGES[PRECHECK_NO_MODEL],
                "model_status": status,
                "task_id": None,
            }
        limit = settings.get("monthly_limit_yuan")
        if limit is None:
            return {
                "ok": False,
                "code": PRECHECK_LIMIT_REQUIRED,
                "message": MESSAGES[ERR_MONTHLY_LIMIT_REQUIRED],
                "model_status": status,
                "task_id": None,
            }

        now = self.clock()
        used_micros = self.review_cost_reader(now)
        limit_micros = int(round(float(limit) * MICROS_PER_YUAN))
        ratio = float(settings.get("cost_warning_ratio") or 0.8)
        used_yuan = used_micros / MICROS_PER_YUAN

        if used_micros >= limit_micros:
            self._note_pause(now, limit)
            return {
                "ok": False,
                "code": PRECHECK_COST_EXCEEDED,
                "message": MESSAGES[PRECHECK_COST_EXCEEDED],
                "model_status": status,
                "task_id": None,
                "used_yuan_estimated": used_yuan,
                "used_yuan_display": format_yuan_amount(used_yuan, kind="estimate"),
                "limit_yuan": limit,
                "estimated": True,
            }

        warning = None
        if used_micros >= int(limit_micros * ratio):
            warning = self._maybe_warn(now, used_yuan, float(limit), limit_micros)

        task_id = None
        if version_id:
            try:
                task = self.repo.create_task(version_id)
                task_id = task["id"]
            except StoreError as exc:
                raise SettingsError(ERR_INVALID_SETTINGS, str(exc)) from exc

        result = {
            "ok": True,
            "code": PRECHECK_OK,
            "message": "",
            "model_status": status,
            "task_id": task_id,
            "used_yuan_estimated": used_yuan,
            "used_yuan_display": format_yuan_amount(used_yuan, kind="estimate"),
            "limit_yuan": limit,
            "estimated": True,
            "single_model_notice": (
                "只配了一个模型，本次结果未经第二个模型复核"
                if status == MODEL_STATUS_SINGLE
                else None
            ),
        }
        if warning:
            result["warning_code"] = PRECHECK_COST_WARNING
            result["warning_message"] = warning
        return result

    def _default_cost_reader(self, now: datetime) -> int:
        # 台账（含测试连接，裁定第 8 条：计入本月估算）+ store 中本月 model_calls
        ledger_micros = self.ledger.month_cost_micros(now=now)
        store_micros = self._store_month_cost_micros(now)
        return ledger_micros + store_micros

    def _store_month_cost_micros(self, now: datetime) -> int:
        try:
            rows = self.repo.list_records("model_calls")
        except Exception:
            return 0
        total = 0
        local_tz = now.tzinfo
        for row in rows:
            created = row.get("created_at")
            if not created:
                continue
            try:
                ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                local = ts.astimezone(local_tz) if local_tz else ts.astimezone()
            except Exception:
                continue
            if local.year == now.year and local.month == now.month:
                total += int(row.get("estimated_cost_micros") or 0)
        return total

    def _maybe_warn(self, now: datetime, used_yuan: float, limit_yuan: float, limit_micros: int) -> str | None:
        key = f"{now.year:04d}-{now.month:02d}"
        state = self._meta.setdefault("cost_warning", {})
        last_key = state.get("month")
        last_limit = state.get("limit_yuan")
        # 每自然月一次；调高上限后重新计算
        if last_key == key and last_limit is not None and float(last_limit) >= limit_yuan:
            return None
        msg = cost_warning_message(used_yuan, limit_yuan)
        state.update({"month": key, "limit_yuan": limit_yuan, "message": msg})
        self._save_meta()
        return msg

    def _note_pause(self, now: datetime, limit_yuan: float) -> None:
        key = f"{now.year:04d}-{now.month:02d}"
        state = self._meta.setdefault("cost_pause", {})
        state.update({"month": key, "limit_yuan": limit_yuan})
        self._save_meta()

    # ---------- 测试连接 ----------
    def test_connection(self, *, role: str = "primary", use_image: bool = False,
                        image_data_url: str | None = None) -> dict[str, Any]:
        if role not in {"primary", "review"}:
            raise SettingsError(ERR_INVALID_SETTINGS, "role 须为 primary 或 review")
        settings = self.repo.get_settings()
        slot = settings.get("primary_model" if role == "primary" else "review_model")
        if not slot:
            return self._test_result(role, TEST_CREDENTIAL_REQUIRED, MESSAGES[TEST_CREDENTIAL_REQUIRED])

        preset = get_preset(slot["provider"])
        try:
            secret = self.credentials.resolve(slot.get("credential_ref"))
        except CredentialError as exc:
            raise SettingsError(exc.code, exc.message) from exc
        if not preset.requires_base_url and not secret:
            return self._test_result(role, TEST_CREDENTIAL_REQUIRED, MESSAGES[TEST_CREDENTIAL_REQUIRED])

        # N1：REST use_image=true 时自动带极小自制图
        if use_image and not image_data_url:
            image_data_url = _tiny_png_data_url()

        client = self._make_client(slot, secret)
        supports_vision = bool(preset.supports_vision) if preset else True
        try:
            result = client.test_connection(
                model=slot["model"],
                supports_vision=supports_vision,
                image_data_url=image_data_url,
            )
        except ModelError as exc:
            self._record_ledger(
                slot, role, None,
                status="error", code=exc.code, image=bool(image_data_url),
                http_status=getattr(exc, "http_status", None),
            )
            msg = MESSAGES.get(exc.code) or other_failure_message(exc.code)
            return self._test_result(role, exc.code, msg, ok=False)

        # M3：按型号取单价
        price = price_for_model(slot["provider"], slot.get("model"))
        cost = estimate_cost_micros(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            input_price_per_m_yuan=price.input_per_m_yuan,
            output_price_per_m_yuan=price.output_per_m_yuan,
        )
        if not price.known and cost.estimated_cost_micros is not None:
            cost_note = "估算（单价未知）"
        else:
            cost_note = cost.note
        self._record_ledger(
            slot, role, result,
            status="ok", code=CODE_OK, image=bool(image_data_url),
            cost_micros=cost.estimated_cost_micros,
        )
        fp = self._fingerprint(slot, secret)
        meta_key = f"test_{role}"
        self._meta[meta_key] = {
            "fingerprint": fp,
            "result_code": TEST_OK,
            "result_message": MESSAGES[TEST_OK],
            "tested_at": self.clock().isoformat(timespec="seconds"),
        }
        self._save_meta()
        yuan = None if cost.estimated_cost_micros is None else cost.estimated_cost_micros / MICROS_PER_YUAN
        return {
            "ok": True,
            "code": TEST_OK,
            "message": MESSAGES[TEST_OK],
            "role": role,
            "model": slot["model"],
            "provider": slot["provider"],
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "estimated_cost_micros": cost.estimated_cost_micros,
            "estimated_cost_yuan": yuan,
            "estimated_cost_display": None if yuan is None else format_yuan_amount(yuan, kind="estimate"),
            "estimated": True,
            "token_unknown": cost.token_unknown,
            "note": cost_note,
        }

    def is_test_result_valid(self, role: str) -> bool:
        settings = self.repo.get_settings()
        slot = settings.get("primary_model" if role == "primary" else "review_model")
        if not slot:
            return False
        try:
            secret = self.credentials.resolve(slot.get("credential_ref"))
        except CredentialError:
            return False
        fp = self._fingerprint(slot, secret)
        saved = self._meta.get(f"test_{role}") or {}
        return bool(saved.get("result_code") == TEST_OK and saved.get("fingerprint") == fp)

    def _make_client(self, slot: dict, secret: SecretStr):
        if self.client_factory:
            return self.client_factory(slot=slot, secret=secret)
        if slot.get("provider") == "mock":
            return MockModelClient(provider="mock", model=slot.get("model") or "mock-vl")
        return OpenAICompatClient(
            provider=slot["provider"],
            base_url=slot.get("base_url") or "",
            api_key=secret,
        )

    def _record_ledger(self, slot, role, result, *, status, code, image, cost_micros=None, http_status=None):
        row = {
            "time": self.clock().isoformat(timespec="seconds"),
            "model": slot.get("model"),
            "role": role,
            "purpose": "test_connection",
            "provider": slot.get("provider"),
            "input_tokens": getattr(result, "input_tokens", None) if result else None,
            "output_tokens": getattr(result, "output_tokens", None) if result else None,
            "estimated_cost_micros": cost_micros,
            "estimated_cost_yuan": None if cost_micros is None else cost_micros / MICROS_PER_YUAN,
            "status": status,
            "code": code,
            "image": image,
        }
        if http_status is not None:
            row["http_status"] = http_status
        if result is not None and getattr(result, "latency_ms", None) is not None:
            row["latency_ms"] = result.latency_ms
        self.ledger.add(row)

    def _test_result(self, role, code, message, ok=False):
        return {"ok": ok, "code": code, "message": message, "role": role}

    def _fingerprint(self, slot: dict, secret: SecretStr) -> str:
        """M9：指纹不含无盐密钥哈希；用 credential_ref + 版本号。"""
        ref = slot.get("credential_ref") or ""
        versions = self._meta.get("credential_versions") or {}
        cred_ver = int(versions.get(ref) or 0)
        # 环境引用无版本时，仅用 ref 本身；显式改密钥会 bump 版本
        raw = json.dumps(
            {
                "provider": slot.get("provider"),
                "model": slot.get("model"),
                "base_url": normalize_base_url(slot.get("base_url")),
                "credential_ref": ref,
                "cred_ver": cred_ver,
                "has_secret": bool(secret),
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _invalidate_tests_if_changed(self, before, after, primary_key, review_key):
        for role, key_arg in (("primary", primary_key), ("review", review_key)):
            b = before.get(f"{role}_model")
            a = after.get(f"{role}_model")
            changed = b != a or key_arg is not None
            if changed:
                self._meta.pop(f"test_{role}", None)
        self._save_meta()

    def _load_meta(self) -> dict:
        if self._meta_path.exists():
            try:
                return json.loads(self._meta_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_meta(self) -> None:
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        self._meta_path.write_text(json.dumps(self._meta, ensure_ascii=False, indent=2), encoding="utf-8")


def dual_model_guard(primary: dict | None, review: dict | None) -> None:
    """供审查引擎调用：禁止同模型双跑。"""
    if not primary or not review:
        return
    if same_base_model(
        primary.get("provider", ""), primary.get("model", ""), primary.get("base_url"),
        review.get("provider", ""), review.get("model", ""), review.get("base_url"),
    ):
        raise SettingsError(ERR_SAME_MODEL, MESSAGES[ERR_SAME_MODEL])
