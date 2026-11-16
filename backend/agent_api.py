"""面向可信本机调用方的只读审查结果。"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.api.review_status import list_issues
from backend.jobs.service import review_progress
from backend.store import Repository, StoreError
from backend.store.errors import ErrorCode
from backend.store.paths import data_root, safe_path


SUMMARY_FIELDS = (
    "task_id", "outcome", "percent", "issue_count", "pending_count",
    "incomplete_count", "unreadable_count", "out_of_scope_count", "cost_paused",
    "drawing_count", "recognized_count",
)
ISSUE_CATEGORIES = {
    "尺寸", "标高", "材料规格", "数量与材料表", "接线", "电缆编号", "设备位号", "编号写法",
    "目录与图签", "图签编号前缀", "目录张数", "详图索引断链", "设计参数",
}
VERSION_ID = re.compile(r"[0-9a-f]{32}")
HTTP_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,}")


class AgentError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.status = status


def _category(value: str) -> str:
    return value if value in ISSUE_CATEGORIES else "其他差异"


def _scopes(rows: list[dict]) -> list[dict]:
    by_category = {}
    for row in rows:
        category = _category(row["category"])
        scope = by_category.setdefault(category, {"category": category, "verified": False,
                                                  "difference": 0, "unverified": 0})
        scope["verified"] |= row["verified"]
        scope["difference"] += row["difference"]
        scope["unverified"] += row["unverified"]
    return list(by_category.values())


class AgentResults:
    def __init__(self, root: Path, version_id: str):
        if not VERSION_ID.fullmatch(version_id):
            raise ValueError("需要 32 位小写十六进制图纸版本 ID")
        self.root = root
        self.version_id = version_id

    def _snapshot(self) -> tuple[dict, list[dict]]:
        try:
            with Repository(self.root, read_only=True) as repo:
                return self._read(repo)
        except StoreError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                raise AgentError("NOT_FOUND", "本机数据库或授权图纸版本不存在", 404) from exc
            raise AgentError("DATA_UNAVAILABLE", "暂时无法读取本机审查结果，请检查数据库后重试", 503) from exc
        except (OSError, ValueError):
            raise AgentError("DATA_UNAVAILABLE", "暂时无法读取本机审查结果，请检查数据库后重试", 503) from None

    def _read(self, repo: Repository) -> tuple[dict, list[dict]]:
        version_id = self.version_id
        version = repo.get("drawing_versions", version_id)
        tasks = repo.list_records("review_tasks", version_id=version_id)
        progress = review_progress(repo, tasks[-1], datetime.now().astimezone()) if tasks else {"outcome": "not_started"}
        summary = {"version_id": version_id, **{key: progress.get(key) for key in SUMMARY_FIELDS},
                   "results_available": False, "has_effective_comparison": False, "comparison_summary": []}
        if not tasks or progress["status"] != "已完成" or progress["cost_paused"]:
            # A queued or interrupted task can contain intermediate issue records.
            summary["issue_count"] = None
            return summary, []

        manifest = safe_path(repo.root, f"projects/{version['project_id']}/cache/import-{version_id}.json")
        if manifest.is_file():
            with manifest.open(encoding="utf-8") as handle:
                progress["unreadable_count"] = max(progress["unreadable_count"], len(json.load(handle).get("unreadable", [])))
        bundle = list_issues(repo, version_id, summary_stub=progress)
        summary.update({key: bundle[key] for key in SUMMARY_FIELDS if key in bundle})
        if summary["outcome"] == "completed" and any(summary[key] for key in
                ("incomplete_count", "unreadable_count", "out_of_scope_count")):
            summary["outcome"] = "partial"
        summary.update(results_available=True,
                       has_effective_comparison=bundle["verification"]["has_effective_comparison"],
                       comparison_summary=_scopes(bundle["comparison_summary"]))
        return summary, bundle["items"]

    def summary(self) -> dict:
        summary, _ = self._snapshot()
        return summary

    def issues(self, limit: int = 50, offset: int = 0) -> dict:
        if not 1 <= limit <= 100 or offset < 0:
            raise AgentError("INVALID_PAGE", "limit 须为 1 至 100，offset 须大于或等于 0", 422)
        summary, rows = self._snapshot()
        if not summary["results_available"]:
            raise AgentError("RESULTS_NOT_READY", "当前暂无已汇总的审查结果", 409)
        items = [{"id": row["id"], "category": _category(row["category"]), "review_status": row["review_status"],
                  "comparison_tag": row["comparison_tag"], "left_unit_id": row["finding"]["left"]["unit_id"],
                  "right_unit_id": row["finding"]["right"]["unit_id"]} for row in rows[offset:offset + limit]]
        return {"version_id": self.version_id, "task_id": summary["task_id"], "outcome": summary["outcome"],
                "total": len(rows), "items": items,
                "next_offset": offset + limit if offset + limit < len(rows) else None}


def create_http_app(results: AgentResults, token: str) -> FastAPI:
    if not HTTP_TOKEN.fullmatch(token):
        raise ValueError("DRAWCHECK_AGENT_TOKEN 须至少 32 个 URL 安全字符")
    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or not compare_digest(credentials.credentials.encode(), token.encode()):
            raise AgentError("UNAUTHORIZED", "需要本机 Agent 访问令牌", 401)

    def authorize_version(version_id: str):
        if version_id != results.version_id:
            raise AgentError("NOT_FOUND", "图纸版本不可访问", 404)

    app = FastAPI(title="DrawCheck Agent", openapi_url=None, docs_url=None, redoc_url=None,
                  dependencies=[Depends(authorize)])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if "origin" in request.headers:
            return JSONResponse({"error": {"code": "BROWSER_ORIGIN", "message": "请使用本机 API 客户端"}}, 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AgentError)
    async def agent_error(_request: Request, exc: AgentError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return JSONResponse({"error": {"code": "INVALID_PAGE", "message": "分页参数无效"}}, 422)

    @app.get("/agent/v1/versions/{version_id}/summary", dependencies=[Depends(authorize_version)])
    def summary():
        return results.summary()

    @app.get("/agent/v1/versions/{version_id}/issues", dependencies=[Depends(authorize_version)])
    def issues(limit: Annotated[int, Query(ge=1, le=100)] = 50,
               offset: Annotated[int, Query(ge=0)] = 0):
        return results.issues(limit, offset)

    return app


def create_mcp_server(results: AgentResults):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = FastMCP("DrawCheck", instructions="只读查询启动时授权的单个本机图纸版本。")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read_only, structured_output=True)
    def get_review_summary() -> dict[str, Any]:
        """查看本机审查进度、问题数和核验覆盖情况。"""
        try:
            return results.summary()
        except AgentError as exc:
            raise ToolError(f"{exc.code}: {exc}") from None

    @server.tool(annotations=read_only, structured_output=True)
    def list_review_issues(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """分页查看已汇总问题的类别、人工复核状态和记录 ID。"""
        try:
            return results.issues(limit, offset)
        except AgentError as exc:
            raise ToolError(f"{exc.code}: {exc}") from None

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="只读访问已授权版本的 DrawCheck 审查结果")
    parser.add_argument("transport", choices=("stdio", "http"))
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--data-dir", type=Path, default=data_root())
    parser.add_argument("--port", type=int, default=8765, help="仅 HTTP，监听 127.0.0.1")
    args = parser.parse_args()
    try:
        results = AgentResults(args.data_dir, args.version_id)
        if args.transport == "stdio":
            create_mcp_server(results).run(transport="stdio")
        else:
            import uvicorn
            app = create_http_app(results, os.environ.get("DRAWCHECK_AGENT_TOKEN", ""))
            uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)
    except ValueError as exc:
        parser.error(str(exc))
    except ModuleNotFoundError as exc:
        if exc.name != "mcp":
            raise
        parser.error("请先安装 requirements-agent.txt 中的 MCP 依赖")


if __name__ == "__main__":
    main()
