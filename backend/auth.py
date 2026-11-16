"""本机令牌校验：请求须带 Authorization: Bearer 或 Cookie drawcheck_token。"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


COOKIE_NAME = "drawcheck_token"
PUBLIC_PATHS = {"/__launch", "/favicon.ico"}


class TokenAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        # 静态前端与启动换票放行；/api/* 必须带令牌
        if path.startswith("/api/"):
            if not self._authorized(request):
                return JSONResponse({"detail": "未授权"}, status_code=401)
        return await call_next(request)

    def _authorized(self, request: Request) -> bool:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
            if got and got == self.token:
                return True
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie and cookie == self.token:
            return True
        return False
