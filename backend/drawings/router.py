from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from backend.store import Repository, StoreError, ErrorCode
from .service import DrawingRemovalService, RemovalError
from .viewer import Region, ViewerError, render_region, resolve_source


def create_router(repository_factory=Repository):
    router = APIRouter()

    def viewer_error(exc):
        if isinstance(exc, ViewerError):
            code, status = exc.code, exc.status
        else:
            code = exc.code.value
            status = {ErrorCode.NOT_FOUND: 404, ErrorCode.ORIGINAL_CHANGED: 409,
                      ErrorCode.DATABASE_BUSY: 503}.get(exc.code, 422)
        return JSONResponse({"error": {"code": code, "message": str(exc)}}, status_code=status)

    @router.get("/api/units/{unit_id}/image-region/info")
    def get_region_info(unit_id: str):
        try:
            with repository_factory() as store:
                return resolve_source(store, unit_id).info()
        except (ViewerError, StoreError) as exc:
            return viewer_error(exc)

    @router.get("/api/units/{unit_id}/image-region")
    def get_image_region(unit_id: str, request: Request):
        try:
            region = Region.from_query(request.query_params)
            with repository_factory() as store:
                source = resolve_source(store, unit_id)
            return Response(render_region(source, region), media_type="image/png",
                            headers={"Cache-Control": "private, no-store"})
        except (ViewerError, StoreError) as exc:
            return viewer_error(exc)

    @router.post("/api/versions/{version_id}/remove-drawings")
    async def remove_drawings(version_id: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        if (not isinstance(body, dict) or set(body) - {"unit_ids", "original_ids", "unreadable_indices"}
                or any(not isinstance(value, list) for value in body.values())):
            return JSONResponse({"error": {"code": "INVALID_DATA", "message": "请选择要移除的图纸或文件"}}, status_code=422)
        try:
            with repository_factory() as store:
                return DrawingRemovalService(store).remove(version_id, **body)
        except RemovalError as exc:
            return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=409)
        except StoreError as exc:
            status = {ErrorCode.NOT_FOUND: 404, ErrorCode.VERSION_READONLY: 409, ErrorCode.DATABASE_BUSY: 503,
                      ErrorCode.DATA_DIR_NOT_WRITABLE: 503, ErrorCode.DISK_FULL: 507}.get(exc.code, 422)
            return JSONResponse({"error": {"code": exc.code.value, "message": str(exc)}}, status_code=status)

    return router
