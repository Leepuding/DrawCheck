"""FastAPI 应用：健康检查 + 静态前端 + 开发期换票入口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.auth import COOKIE_NAME, TokenAuthMiddleware

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def create_app(token: str) -> FastAPI:
    app = FastAPI(title="图纸审查", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token
    from backend.revisions.router import create_router
    app.include_router(create_router())
    from backend.drawings.router import create_router as drawings_router
    app.include_router(drawings_router())
    app.add_middleware(TokenAuthMiddleware, token=token)

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/health/components")
    async def get_components():
        import asyncio
        from backend.components import components
        return await asyncio.to_thread(components)

    @app.get("/__launch")
    async def launch(request: Request) -> RedirectResponse:
        """开发/测试用：用一次性查询参数换 HttpOnly Cookie，再跳到 /。产品界面不展示令牌。"""
        t = request.query_params.get("t", "")
        if not t or t != token:
            return RedirectResponse(url="/", status_code=302)
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(
            key=COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return resp

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    assets = FRONTEND_DIR
    if (assets / "css").is_dir():
        app.mount("/css", StaticFiles(directory=str(assets / "css")), name="css")
    if (assets / "js").is_dir():
        app.mount("/js", StaticFiles(directory=str(assets / "js")), name="js")
    if (assets / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets / "assets")), name="assets")

    # DC-08：设置 / 测试连接 / 前置检查（只加不改既有健康检查与鉴权）
    @app.get("/api/settings")
    async def get_settings() -> dict:
        from backend.settings import SettingsService, SettingsError
        from backend.settings.credentials import CredentialError
        from backend.store import Repository
        from backend.store.errors import StoreError
        try:
            with Repository() as repo:
                return SettingsService(repo).get_settings_view()
        except SettingsError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except CredentialError as exc:
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=422)
        except StoreError as exc:
            return JSONResponse({"error": {"code": "INVALID_SETTINGS", "message": str(exc)}}, status_code=422)
        except Exception:
            return JSONResponse({"error": {"code": "OTHER", "message": "连接失败（错误码：OTHER），请稍后再试"}}, status_code=500)

    @app.patch("/api/settings")
    async def patch_settings(request: Request) -> dict:
        from backend.settings import SettingsService, SettingsError
        from backend.settings.credentials import CredentialError
        from backend.store import Repository
        from backend.store.errors import StoreError
        body = await request.json()
        primary_key = body.pop("primary_api_key", None)
        review_key = body.pop("review_api_key", None)
        try:
            with Repository() as repo:
                return SettingsService(repo).save_settings(
                    body, primary_api_key=primary_key, review_api_key=review_key)
        except SettingsError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except CredentialError as exc:
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=422)
        except StoreError as exc:
            return JSONResponse({"error": {"code": "INVALID_SETTINGS", "message": str(exc)}}, status_code=422)
        except Exception:
            return JSONResponse({"error": {"code": "OTHER", "message": "连接失败（错误码：OTHER），请稍后再试"}}, status_code=500)

    @app.post("/api/settings/test-connection")
    async def test_connection(request: Request):
        from backend.settings import SettingsService, SettingsError
        from backend.settings.credentials import CredentialError
        from backend.store import Repository
        from backend.store.errors import StoreError
        body = await request.json()
        try:
            with Repository() as repo:
                return SettingsService(repo).test_connection(
                    role=body.get("role", "primary"),
                    use_image=bool(body.get("use_image")),
                )
        except SettingsError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except CredentialError as exc:
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=422)
        except StoreError as exc:
            return JSONResponse({"error": {"code": "INVALID_SETTINGS", "message": str(exc)}}, status_code=422)
        except Exception:
            return JSONResponse({"error": {"code": "OTHER", "message": "连接失败（错误码：OTHER），请稍后再试"}}, status_code=500)

    @app.post("/api/settings/precheck")
    async def precheck(request: Request):
        from backend.jobs import JobError, JobService
        from backend.settings import SettingsService, SettingsError
        from backend.settings.credentials import CredentialError
        from backend.store import Repository
        from backend.store.errors import StoreError
        body = await request.json()
        try:
            with Repository() as repo:
                if body.get("disclosure_version_id"):
                    scope = {"retry_unit_id": body["retry_unit_id"]} if body.get("retry_unit_id") is not None else {}
                    return JobService(repo).prepare_disclosure(body["disclosure_version_id"], **scope)
                return SettingsService(repo).precheck_start_review(version_id=body.get("version_id"))
        except SettingsError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except JobError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except CredentialError as exc:
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=422)
        except StoreError as exc:
            return JSONResponse({"error": {"code": "INVALID_SETTINGS", "message": str(exc)}}, status_code=422)
        except Exception:
            return JSONResponse({"error": {"code": "OTHER", "message": "连接失败（错误码：OTHER），请稍后再试"}}, status_code=500)


    # DC-09：看图识别 / 目录 / 改名（只加不改既有路由）
    @app.post("/api/extract/unit")
    async def extract_unit(request: Request):
        import os
        if os.environ.get("DRAWCHECK_ENABLE_TEST_ROUTES") != "1":
            return JSONResponse({"error": {"code": "USE_REVIEW_TASK", "message": "请通过开始审查识别图纸"}}, status_code=422)
        from backend.extract import ExtractService
        from backend.extract.errors import ExtractError
        from backend.store import Repository
        from backend.store.errors import StoreError
        body = await request.json()
        try:
            with Repository() as repo:
                result = ExtractService(repo).extract_unit(body["unit_id"], persist=bool(body.get("persist", True)))
                return result.as_dict()
        except ExtractError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except StoreError as exc:
            return JSONResponse({"error": {"code": str(exc.code), "message": str(exc)}}, status_code=422)
        except Exception:
            return JSONResponse({"error": {"code": "OTHER", "message": "识别失败（错误码：OTHER）"}}, status_code=500)

    @app.get("/api/catalog/{version_id}")
    async def get_catalog(version_id: str):
        from backend.catalog import CatalogService
        from backend.catalog.errors import CatalogError
        from backend.store import Repository
        from backend.store.errors import StoreError
        try:
            with Repository() as repo:
                return {"rows": CatalogService(repo).list_catalog(version_id)}
        except CatalogError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except StoreError as exc:
            return JSONResponse({"error": {"code": str(exc.code), "message": str(exc)}}, status_code=422)
        except Exception:
            return JSONResponse({"error": {"code": "OTHER", "message": "目录查询失败（错误码：OTHER）"}}, status_code=500)

    @app.post("/api/catalog/rename")
    async def catalog_rename(request: Request):
        from backend.catalog import CatalogService
        from backend.catalog.errors import CatalogError
        from backend.store import Repository
        from backend.store.errors import StoreError
        body = await request.json()
        try:
            with Repository() as repo:
                return CatalogService(repo).rename(
                    unit_id=body.get("unit_id"),
                    row_id=body.get("row_id"),
                    version_id=body.get("version_id"),
                    drawing_number=body.get("drawing_number"),
                    name=body.get("name"),
                    specialty=body.get("specialty"),
                )
        except CatalogError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except StoreError as exc:
            return JSONResponse({"error": {"code": str(exc.code), "message": str(exc)}}, status_code=422)
        except Exception:
            return JSONResponse({"error": {"code": "OTHER", "message": "改名失败（错误码：OTHER）"}}, status_code=500)

    # DC-14：导入和目录薄路由；设置仍由上方 DC-08 独立处理。
    import asyncio
    import json
    import tempfile
    import threading
    from uuid import uuid4
    from starlette.exceptions import HTTPException
    from starlette.datastructures import UploadFile
    from backend.importer import ImportService
    from backend.store import Repository, StoreError

    importer = None
    cleanup_tasks = set()
    selections = {}
    selection_lock = threading.Lock()

    def import_service():
        nonlocal importer
        if importer is None:
            importer = ImportService()
        return importer

    def register_import_selection(paths):
        """只由桌面原生选择器调用；HTTP 不接受任意本机路径。"""
        identifier = uuid4().hex
        with selection_lock:
            selections.clear()
            selections[identifier] = list(paths)
        return identifier

    app.state.register_import_selection = register_import_selection

    async def close_importer():
        if importer is not None:
            await asyncio.to_thread(importer.close)
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    app.router.on_shutdown.append(close_importer)

    def route_error(code, message, status=422):
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

    def store_error(exc):
        code = exc.code.value
        status = {"NOT_FOUND": 404, "VERSION_READONLY": 409, "DATABASE_BUSY": 503,
                  "DATA_DIR_NOT_WRITABLE": 503, "DISK_FULL": 507}.get(code, 422)
        message = {"NOT_FOUND": "图纸记录不存在", "VERSION_READONLY": "旧版记录只读"}.get(code, "图纸目录暂时无法保存，请重试")
        return route_error(code, message, status)

    @app.post("/api/import", status_code=202)
    async def submit_import(request: Request, version_id: str | None = None):
        service = import_service()
        staging = None
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = await request.json()
                except (ValueError, json.JSONDecodeError):
                    return route_error("INVALID_IMPORT", "请选择要导入的文件")
                if not isinstance(body, dict) or set(body) != {"selection_id"} or not isinstance(body["selection_id"], str):
                    return route_error("INVALID_IMPORT", "请通过文件选择器导入图纸")
                with selection_lock:
                    paths = selections.pop(body["selection_id"], None)
                if not paths:
                    return route_error("INVALID_SELECTION", "文件选择已失效，请重新选择")
            else:
                staging = tempfile.TemporaryDirectory(prefix="drawcheck-upload-")
                paths, total = [], 0
                async with request.form(max_files=service.limits.max_files, max_fields=0) as form:
                    for key, upload in form.multi_items():
                        if key != "files" or not isinstance(upload, UploadFile):
                            return route_error("INVALID_IMPORT", "请选择要导入的文件")
                        # 文件夹上传保留相对目录；拒绝穿越、绝对路径与重复目标。
                        name = (upload.filename or "").replace("\\", "/")
                        parts = name.split("/")
                        if not name or any(part in ("", ".", "..") or ":" in part or "\x00" in part for part in parts):
                            return route_error("INVALID_FILENAME", "文件名无效，请重新选择")
                        target = Path(staging.name).joinpath(*parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():
                            return route_error("DUPLICATE_FILENAME", "存在同名文件，请改选文件夹导入")
                        with target.open("xb") as output:
                            while chunk := await upload.read(1024 * 1024):
                                total += len(chunk)
                                if total > service.limits.max_bytes:
                                    return route_error("IMPORT_LIMIT", "文件总大小超过导入上限")
                                output.write(chunk)
                        paths.append(target)
                if not paths:
                    return route_error("EMPTY_SELECTION", "请选择要导入的文件")
            if version_id:
                from backend.revisions import RevisionService
                with Repository() as repo:
                    repo.get("drawing_versions", version_id)
                    revision = RevisionService(repo).comparison(version_id)
                    if not revision.get("is_revision") or revision.get("status") != "draft":
                        return route_error("INVALID_REVISION", "请先选择导入修订版", 409)
                    repo._writable_version(version_id)
            project_name = Path(paths[0]).stem if paths else "导入项目"
            job_id = service.submit(paths, version_id=version_id, project_name=project_name)
            if staging is not None:
                owned_staging = staging
                staging = None

                async def remove_staging():
                    try:
                        await asyncio.to_thread(service.wait, job_id)
                    finally:
                        owned_staging.cleanup()

                task = asyncio.create_task(remove_staging())
                cleanup_tasks.add(task)
                task.add_done_callback(cleanup_tasks.discard)
            return {"job_id": job_id}
        except StoreError as exc:
            return store_error(exc)
        except HTTPException:
            return route_error("INVALID_IMPORT", "文件选择无效或超过导入上限")
        except OSError:
            return route_error("IMPORT_IO", "暂时无法读取所选文件，请重试", 503)
        finally:
            if staging is not None:
                staging.cleanup()

    @app.get("/api/import/{job_id}")
    async def get_import(job_id: str):
        try:
            snapshot = import_service().get_job(job_id)
        except KeyError:
            return route_error("NOT_FOUND", "导入记录不存在，请重新选择文件", 404)
        # Progress is explicitly allowlisted; never expose source paths or raw errors.
        return {"job_id": job_id, "status": snapshot["status"], "result": snapshot["result"],
                **{key: snapshot.get(key) for key in ("phase_key", "phase", "processed_count", "total_count",
                    "progress_unit", "phase_percent", "percent", "elapsed_seconds", "eta_seconds",
                    "eta_scope", "estimate_basis", "current_file")},
                "error": ({"code": snapshot["error"]["code"], "message": "导入未完成，请检查文件后重试"}
                          if snapshot["error"] else None)}

    @app.get("/api/units")
    async def get_units(version_id: str):
        try:
            with Repository() as repo:
                repo.get("drawing_versions", version_id)
                return {"items": repo.list_records("drawing_units", version_id=version_id)}
        except StoreError as exc:
            return store_error(exc)

    @app.patch("/api/units/{unit_id}")
    async def rename_unit(unit_id: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            return route_error("INVALID_DATA", "请填写图名")
        if (not isinstance(body, dict) or set(body) - {"name", "drawing_number"}
                or not isinstance(body.get("name"), str) or not body["name"].strip()
                or len(body["name"]) > 500
                or ("drawing_number" in body and not isinstance(body["drawing_number"], str))):
            return route_error("INVALID_DATA", "请填写有效图名（不超过 500 字）")
        try:
            with Repository() as repo:
                unit = repo.get("drawing_units", unit_id)
                return repo.update_unit_directory(unit_id, name=body["name"].strip(),
                                                  drawing_number=body.get("drawing_number", unit["drawing_number"]))
        except StoreError as exc:
            return store_error(exc)


    # Real review runs off the HTTP event loop; one worker serializes local writes.
    active_reviews = {}
    review_lock = threading.RLock()
    review_work_lock = threading.Lock()
    review_closing = threading.Event()

    def run_review(version_id, task_id):
        from backend.jobs import JobService
        try:
            with review_work_lock:
                if review_closing.is_set():
                    return
                with Repository() as repo:
                    service = JobService(repo)
                    service.extras["cancel_event"] = review_closing
                    service.run_task(task_id)
        except Exception:
            with Repository() as repo:
                task = repo.get("review_tasks", task_id)
                repo.update_task(task_id, status="失败", checkpoints=task["checkpoints"],
                    incomplete=task["incomplete"] + [{"reason":"审查中断，请重试", "category":"incomplete"}],
                    estimated_cost_micros=task["estimated_cost_micros"])
        finally:
            with review_lock:
                active_reviews.pop(version_id, None)

    async def close_reviews():
        # Checkpoints are durable. Closing the window must not wait for a remote
        # model timeout; daemon workers cannot keep the desktop process alive.
        review_closing.set()
    app.router.on_shutdown.append(close_reviews)

    # DC-10：审查任务 / 进度 / 未完成探测
    @app.post("/api/versions/{version_id}/tasks")
    async def start_review_task(version_id: str, request: Request):
        from backend.jobs import JobError, JobService
        from backend.settings import SettingsService, SettingsError
        from backend.settings.credentials import CredentialError
        from backend.store import Repository
        from backend.store.errors import StoreError
        try:
            body = await request.json() if await request.body() else {}
            with Repository() as repo:
                with review_lock:
                    if version_id in active_reviews:
                        result = JobService(repo).get_progress(active_reviews[version_id])
                        result["already_running"] = True
                        return result
                    scope = {"retry_unit_id": body["retry_unit_id"]} if body.get("retry_unit_id") is not None else {}
                    result = JobService(repo, settings_service=SettingsService(repo)).start_review(
                        version_id, run=False, disclosure_id=body.get("disclosure_id") or "", **scope)
                    if result.get("ok") is not False and result.get("task_id"):
                        active_reviews[version_id] = result["task_id"]
                        threading.Thread(target=run_review, args=(version_id, result["task_id"]),
                                         name="drawcheck-review", daemon=True).start()
                if result.get("ok") is False and result.get("error"):
                    code = result["error"].get("code", "INVALID_TASK")
                    status = 409 if code in {"COST_EXCEEDED"} else 422
                    if code == "NO_MODEL":
                        status = 422
                    return JSONResponse({"error": result["error"], **{k: result.get(k) for k in ("task_id", "model_status", "already_running") if k in result}}, status_code=status)
                # 防重：200 + already_running；新建/续审完成亦 200
                return result
        except JobError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except SettingsError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except CredentialError as exc:
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=422)
        except StoreError as exc:
            return JSONResponse({"error": {"code": str(exc.code), "message": "审查任务无法创建"}}, status_code=422)
        except Exception:
            return JSONResponse({"error": {"code": "OTHER", "message": "审查启动失败（错误码：OTHER）"}}, status_code=500)

    @app.get("/api/tasks/{task_id}")
    async def get_review_task(task_id: str):
        from backend.jobs import JobError, JobService
        from backend.store import Repository
        from backend.store.errors import StoreError
        try:
            with Repository() as repo:
                return JobService(repo).get_progress(task_id)
        except JobError as exc:
            status = 404 if exc.code == "NOT_FOUND" else 422
            return JSONResponse(exc.as_error(), status_code=status)
        except StoreError as exc:
            return JSONResponse({"error": {"code": str(exc.code), "message": "审查任务不存在"}}, status_code=404)
        except Exception:
            return JSONResponse({"error": {"code": "OTHER", "message": "进度查询失败（错误码：OTHER）"}}, status_code=500)

    @app.get("/api/versions/{version_id}/review/resume-probe")
    async def resume_probe(version_id: str):
        from backend.jobs import JobError, JobService
        from backend.store import Repository
        from backend.store.errors import StoreError
        try:
            with Repository() as repo:
                return JobService(repo).probe_incomplete(version_id)
        except JobError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except StoreError as exc:
            return JSONResponse({"error": {"code": str(exc.code), "message": "无法探测未完成审查"}}, status_code=422)
        except Exception:
            return JSONResponse({"error": {"code": "OTHER", "message": "续审探测失败（错误码：OTHER）"}}, status_code=500)

    # DC-15：问题列表 / 复核 / 单元审查图（只加路由，不改既有模块行为）
    @app.get("/api/issues")
    async def get_issues(version_id: str, incomplete_count: int = 0, unreadable_count: int = 0,
                         out_of_scope_count: int = 0, cost_paused: bool = False, revised: bool = False):
        from backend.api.review_status import list_issues
        from backend.store import Repository, StoreError
        try:
            with Repository() as repo:
                from backend.jobs import JobService
                tasks = repo.list_records("review_tasks", version_id=version_id)
                summary = JobService(repo).get_progress(tasks[-1]["id"]) if tasks else {"incomplete_count": 1}

                version = repo.get("drawing_versions", version_id)
                manifest = repo.root / "projects" / version["project_id"] / "cache" / f"import-{version_id}.json"
                if manifest.is_file():
                    summary["unreadable_count"] = max(summary.get("unreadable_count", 0), len(json.loads(manifest.read_text()).get("unreadable", [])))
                summary["revised"] = version["sequence"] > 1
                return list_issues(repo, version_id, summary_stub=summary)
        except StoreError as exc:
            return store_error(exc)

    @app.patch("/api/issues/{issue_id}")
    async def patch_issue_route(issue_id: str, request: Request):
        from backend.api.review_status import patch_issue
        from backend.store import Repository, StoreError
        try:
            body = await request.json()
        except ValueError:
            return route_error("INVALID_DATA", "请求体无效")
        try:
            with Repository() as repo:
                return patch_issue(repo, issue_id, body if isinstance(body, dict) else {})
        except StoreError as exc:
            return store_error(exc)

    @app.post("/api/issues/seed-mock")
    async def seed_mock_issues_route(request: Request):
        import os
        if os.environ.get("DRAWCHECK_ENABLE_TEST_ROUTES") != "1":
            return route_error("NOT_FOUND", "未找到", 404)
        """无 DC-11 时注入 Mock 问题；契约见 docs/API.md DC-15。"""
        from backend.api.review_status import seed_mock_issues
        from backend.store import Repository, StoreError
        try:
            body = await request.json()
        except ValueError:
            return route_error("INVALID_DATA", "请求体无效")
        if not isinstance(body, dict) or not isinstance(body.get("version_id"), str) or not body["version_id"]:
            return route_error("INVALID_DATA", "请提供 version_id")
        try:
            with Repository() as repo:
                return seed_mock_issues(repo, body["version_id"], force=bool(body.get("force")))
        except StoreError as exc:
            return store_error(exc)


    # DC-16：待你确认列表 / 确认·无法确定 / Mock seed（只加路由）
    @app.get("/api/pending")
    async def get_pending(version_id: str):
        from backend.api.pending import list_pending
        from backend.store import Repository, StoreError
        try:
            with Repository() as repo:
                return list_pending(repo, version_id)
        except StoreError as exc:
            return store_error(exc)

    @app.patch("/api/pending/{pending_id}")
    async def patch_pending_route(pending_id: str, request: Request):
        from backend.api.pending import confirm_pending_item
        from backend.store import Repository, StoreError
        try:
            body = await request.json()
        except ValueError:
            return route_error("INVALID_DATA", "请求体无效")
        try:
            with Repository() as repo:
                return confirm_pending_item(repo, pending_id, body if isinstance(body, dict) else {})
        except StoreError as exc:
            code = exc.code.value
            status = {"NOT_FOUND": 404, "VERSION_READONLY": 409, "DATABASE_BUSY": 503}.get(code, 422)
            return route_error(code, str(exc) or "待确认保存失败", status)

    @app.post("/api/pending/seed-mock")
    async def seed_mock_pending_route(request: Request):
        import os
        if os.environ.get("DRAWCHECK_ENABLE_TEST_ROUTES") != "1":
            return route_error("NOT_FOUND", "未找到", 404)
        """无识别结果时注入模糊待确认；契约见 docs/API.md DC-16。"""
        from backend.api.pending import seed_mock_pending
        from backend.store import Repository, StoreError
        try:
            body = await request.json()
        except ValueError:
            return route_error("INVALID_DATA", "请求体无效")
        if not isinstance(body, dict) or not isinstance(body.get("version_id"), str) or not body["version_id"]:
            return route_error("INVALID_DATA", "请提供 version_id")
        try:
            with Repository() as repo:
                return seed_mock_pending(repo, body["version_id"], force=bool(body.get("force")))
        except StoreError as exc:
            return store_error(exc)

    @app.get("/api/units/{unit_id}/image")
    async def get_unit_image(unit_id: str):
        from backend.api.review_status import resolve_unit_image
        from backend.store import Repository, StoreError
        from fastapi.responses import FileResponse
        try:
            with Repository() as repo:
                path = resolve_unit_image(repo, unit_id)
            media = "image/png" if path.suffix.lower() == ".png" else "application/octet-stream"
            return FileResponse(path, media_type=media, filename=path.name)
        except StoreError as exc:
            return store_error(exc)

    @app.post("/api/versions/{version_id}/exports")
    async def generate_exports(version_id: str):
        from backend.export import ExportService
        def generate():
            with Repository() as repo:
                return ExportService(repo).generate(version_id)
        try:
            return await asyncio.to_thread(generate)
        except StoreError as exc:
            return route_error(exc.code.value, str(exc), 422)
        except OSError:
            return route_error("EXPORT_IO", "导出目录不可写或磁盘空间不足，请检查后重试", 503)
        except Exception:
            return route_error("EXPORT_FAILED", "导出未完成，请检查原图后重试", 500)

    @app.get("/api/exports/{export_id}/download")
    async def download_export(export_id: str):
        from backend.export import ExportService
        try:
            with Repository() as repo:
                path = ExportService(repo).resolve(export_id)
            return FileResponse(path, filename=path.name)
        except StoreError as exc:
            return store_error(exc)

    @app.post("/api/exports/{export_id}/reveal")
    async def reveal_export(export_id: str):
        from backend.export import ExportService
        try:
            with Repository() as repo:
                return ExportService(repo).reveal(export_id)
        except StoreError as exc:
            return store_error(exc)
        except OSError:
            return route_error("REVEAL_FAILED", "暂时无法打开所在文件夹", 503)

    @app.post("/api/issues/{issue_id}/retry-review")
    async def retry_issue_review(issue_id: str):
        from backend.jobs import JobError, JobService
        def retry():
            with Repository() as repo:
                return JobService(repo).retry_issue_review(issue_id)
        try:
            return await asyncio.to_thread(retry)
        except JobError as exc:
            return JSONResponse(exc.as_error(), status_code=422)
        except StoreError as exc:
            return store_error(exc)
        except Exception:
            return route_error("REVIEW_FAILED", "复核未完成，请稍后重试", 422)

    @app.exception_handler(404)
    async def not_found(_request: Request, _exc) -> JSONResponse:
        return JSONResponse({"detail": "未找到"}, status_code=404)

    return app
