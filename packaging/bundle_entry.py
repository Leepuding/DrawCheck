"""PyInstaller entry: desktop window, diagnostics, and the existing DWG subprocess protocol."""
from __future__ import annotations

from pathlib import Path
import runpy
import subprocess
import sys

from bundle_support import configure_runtime, diagnose, self_test, write_report

ALLOWED_WORKERS = {"backend.parsers.dwg._worker", "backend.parsers.dwg._limit_exec",
                   "backend.drawings._viewer_worker"}


def dispatch_worker(argv: list[str]) -> int | None:
    if not argv or argv[0] != "-m":
        return None
    if len(argv) < 2 or argv[1] not in ALLOWED_WORKERS:
        return 2
    module, arguments = argv[1], argv[2:]
    if module.endswith("._limit_exec"):
        # Keep the process tree intact for taskkill on Windows and killpg on macOS.
        # The frozen executable supports only the explicitly listed worker modules.
        if len(arguments) < 2:
            return 2
        try:
            if int(arguments[0]) < 64:
                return 2
        except ValueError:
            return 2
        return subprocess.call(arguments[1:])
    sys.argv = [module, *arguments]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_runtime()
    argv = list(sys.argv[1:] if argv is None else argv)
    worker_result = dispatch_worker(argv)
    if worker_result is not None:
        return worker_result
    if len(argv) == 2 and argv[0] in ("--diagnose", "--self-test"):
        payload = self_test() if argv[0] == "--self-test" else diagnose()
        write_report(argv[1], payload)
        return 0 if payload["ok"] else 1
    if argv:  # --browser is a development option, not a product launch mode.
        return 2
    from app.launcher import main as launch
    return launch([])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Do not write exception messages/tracebacks that may contain a launch token.
        if sys.platform == "win32" and len(sys.argv) == 1:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,
                f"图纸审查未能启动（{type(exc).__name__}）。请重新安装或联系维护人员。",
                "图纸审查", 0x10)
        raise SystemExit(1)
