"""Thin local API; authentication remains owned by the application middleware."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.store import Repository, StoreError, ErrorCode
from .service import RevisionService


def create_router(repository_factory=Repository):
    router = APIRouter()

    def call(method, *args, **kwargs):
        try:
            with repository_factory() as store:
                return getattr(RevisionService(store), method)(*args, **kwargs)
        except StoreError as exc:
            return JSONResponse({"error": {"code": exc.code.value, "message": str(exc)}}, status_code=404 if exc.code == ErrorCode.NOT_FOUND else 409 if exc.code == ErrorCode.VERSION_READONLY else 422)

    @router.post("/api/versions/{version_id}/revisions")
    async def create_revision(version_id: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict) or set(body) - {"name"}:
            return JSONResponse({"error": {"code": "INVALID_DATA", "message": "修订请求无效"}}, status_code=422)
        return call("create_revision", version_id, body.get("name", "修订版"))

    @router.post("/api/versions/{version_id}/revision/prepare")
    async def prepare(version_id: str):
        return call("prepare", version_id)

    @router.get("/api/versions/{version_id}/revision")
    async def comparison(version_id: str):
        return call("comparison", version_id)

    @router.get("/api/history")
    async def history():
        return call("history")

    @router.post("/api/history/open")
    async def open_project(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        if (not isinstance(body, dict) or not isinstance(body.get("project_id"), str)
                or set(body) - {"project_id", "version_id", "source_version_id"}
                or any(key in body and not isinstance(body[key], str) for key in ("version_id", "source_version_id"))
                or ("version_id" in body and "source_version_id" in body)):
            return JSONResponse({"error": {"code": "INVALID_DATA", "message": "请选择历史项目"}}, status_code=422)
        return call("open_project", body["project_id"], body.get("version_id"), body.get("source_version_id"))

    return router
