"""本机 uvicorn 启停：仅 127.0.0.1，端口随机。"""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional

import uvicorn

from backend.app import create_app


@dataclass
class RunningServer:
    host: str
    port: int
    token: str
    server: uvicorn.Server
    thread: threading.Thread

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def wait_ready(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.02)
        raise TimeoutError("后端未在时限内就绪")

    def stop(self, join_timeout: float = 3.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=join_timeout)


def pick_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def start_server(token: str, host: str = "127.0.0.1", port: Optional[int] = None) -> RunningServer:
    if host not in ("127.0.0.1", "::1"):
        raise ValueError("后端只允许监听本机回环地址")
    if port is None:
        port = pick_free_port(host)
    app = create_app(token)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        server.run()

    thread = threading.Thread(target=_run, name="drawcheck-backend", daemon=True)
    thread.start()
    running = RunningServer(host=host, port=port, token=token, server=server, thread=thread)
    running.wait_ready()
    return running
