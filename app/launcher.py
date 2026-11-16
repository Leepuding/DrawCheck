"""图纸审查桌面壳：同进程后端 + pywebview 窗口 + 单实例。"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional

# 保证以仓库根为 sys.path，便于 `python -m app.launcher` / scripts/dev
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.server import RunningServer, start_server

APP_TITLE = "图纸审查"
LOCK_DIR_NAME = "drawcheck-instance"
FOCUS_TIMEOUT = 2.0


def _user_runtime_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return Path(base) / "DrawCheck" / "run"
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / LOCK_DIR_NAME
    return Path(tempfile.gettempdir()) / LOCK_DIR_NAME


class InstanceLock:
    """文件锁 + 本机回环聚焦端口：第二次启动只激活已有窗口。"""

    def __init__(self, runtime_dir: Optional[Path] = None):
        self.runtime_dir = runtime_dir or _user_runtime_dir()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.runtime_dir / "instance.lock"
        self.meta_path = self.runtime_dir / "instance.json"
        self._fh = None
        self._focus_sock: Optional[socket.socket] = None
        self._focus_thread: Optional[threading.Thread] = None
        self._on_focus: Optional[Callable[[], None]] = None
        self._closed = False

    def acquire(self) -> bool:
        """拿到锁返回 True；若已有实例则发聚焦命令并返回 False。"""
        self._fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            self._try_lock()
        except OSError:
            self._fh.close()
            self._fh = None
            self._signal_focus()
            return False
        return True

    def _try_lock(self) -> None:
        if sys.platform == "win32":
            import msvcrt

            self._fh.seek(0)
            self._fh.write("0")
            self._fh.flush()
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def start_focus_server(self, on_focus: Callable[[], None]) -> int:
        self._on_focus = on_focus
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = int(sock.getsockname()[1])
        self._focus_sock = sock
        meta = {"pid": os.getpid(), "focus_port": port}
        self.meta_path.write_text(json.dumps(meta), encoding="utf-8")

        def _serve() -> None:
            sock.settimeout(0.5)
            while not self._closed:
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with conn:
                    try:
                        data = conn.recv(64)
                        if data.strip() == b"FOCUS" and self._on_focus:
                            self._on_focus()
                        conn.sendall(b"OK")
                    except OSError:
                        pass

        self._focus_thread = threading.Thread(target=_serve, name="drawcheck-focus", daemon=True)
        self._focus_thread.start()
        return port

    def _signal_focus(self) -> bool:
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            port = int(meta["focus_port"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=FOCUS_TIMEOUT) as conn:
                conn.sendall(b"FOCUS")
                conn.recv(16)
            return True
        except OSError:
            return False

    def release(self) -> None:
        self._closed = True
        if self._focus_sock is not None:
            try:
                self._focus_sock.close()
            except OSError:
                pass
            self._focus_sock = None
        if self._fh is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        try:
            if self.meta_path.exists():
                self.meta_path.unlink()
        except OSError:
            pass


class ImportBridge:
    """DC-14：原生对话框只交出一次性选择凭证，不向页面回传本机路径。"""

    def __init__(self, application):
        self._application = application

    def choose_import(self, folder=False):
        import webview

        application = self._application
        if application._window is None or application.server is None:
            return None
        try:
            kind = webview.FOLDER_DIALOG if folder else webview.OPEN_DIALOG
            paths = application._window.create_file_dialog(kind, allow_multiple=not folder)
            if not paths:
                return None
            register = application.server.server.config.app.state.register_import_selection
            return {"selection_id": register(paths)}
        except Exception:
            return {"error": "文件选择未完成"}


class Application:
    def __init__(self, browser: bool = False, token: Optional[str] = None):
        self.browser = browser
        self.token = token or secrets.token_urlsafe(24)
        self.server: Optional[RunningServer] = None
        self.lock = InstanceLock()
        self._window = None
        self._exit_code = 0

    def start_backend(self) -> RunningServer:
        self.server = start_server(self.token, host="127.0.0.1", port=None)
        return self.server

    def launch_url(self) -> str:
        assert self.server is not None
        return f"{self.server.base_url}/__launch?t={self.token}"

    def run(self) -> int:
        if not self.lock.acquire():
            # 已有实例被激活；本进程直接退出
            return 0
        try:
            self.start_backend()
            if self.browser:
                return self._run_browser()
            return self._run_webview()
        finally:
            self.shutdown()

    def _run_browser(self) -> int:
        assert self.server is not None
        url = self.launch_url()
        webbrowser.open(url)
        print(f"{APP_TITLE} 开发浏览器模式已启动（仅测试用）。", flush=True)
        print("按 Ctrl+C 结束。", flush=True)
        try:
            while self.server.thread.is_alive():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        return 0

    def _run_webview(self) -> int:
        try:
            import webview
        except ImportError:
            print("未安装 pywebview，请改用 scripts/dev --browser", file=sys.stderr)
            self._exit_code = 2
            return self._exit_code

        assert self.server is not None
        url = self.launch_url()
        window = webview.create_window(APP_TITLE, url=url, width=1280, height=800, js_api=ImportBridge(self))
        self._window = window

        def focus() -> None:
            try:
                window.show()
                window.restore()
            except Exception:
                pass

        self.lock.start_focus_server(focus)

        def on_closed() -> None:
            # 关窗即停后端
            if self.server is not None:
                self.server.server.should_exit = True

        try:
            window.events.closed += on_closed
        except Exception:
            # 旧版 API 兜底：轮询窗口存活
            pass

        webview.start()
        # start() 返回后窗口已关
        if self.server is not None:
            self.server.server.should_exit = True
        return self._exit_code

    def shutdown(self) -> None:
        if self.server is not None:
            self.server.stop(join_timeout=3.0)
            # 再等最多到 3 秒内线程退出（验收：关窗后后端 3 秒内退出）
            deadline = time.time() + 3.0
            while self.server.thread.is_alive() and time.time() < deadline:
                time.sleep(0.05)
            self.server = None
        self.lock.release()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="图纸审查开发启动器")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="用系统浏览器打开前端（仅测试/开发，不进安装包）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    app = Application(browser=args.browser)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
