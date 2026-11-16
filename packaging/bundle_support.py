"""Frozen runtime setup; kept separate from the shared application launcher."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def configure_runtime() -> None:
    if not getattr(sys, "frozen", False):
        return
    # Product executables never silently inherit development/live-provider switches.
    os.environ.pop("DRAWCHECK_DEV", None)
    os.environ.pop("DRAWCHECK_LIVE_MODEL", None)
    os.environ.pop("DRAWCHECK_ENABLE_TEST_ROUTES", None)
    if sys.platform == "darwin":
        # Finder does not inherit an interactive shell's Homebrew PATH.
        os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", os.defpath)
        os.environ["PYWEBVIEW_GUI"] = "cocoa"
    if sys.platform == "win32":
        vendor = resource_root() / "third_party" / "vendor"
        bins = [vendor / "poppler/bin", vendor / "libredwg/bin", vendor / "7zip"]
        os.environ["PATH"] = os.pathsep.join(map(str, bins)) + os.pathsep + os.environ.get("PATH", "")
        os.environ["DRAWCHECK_LIBREDWG_BIN"] = str(bins[1])
        os.environ["DRAWCHECK_7Z"] = str(bins[2] / "7z.exe")
        os.environ["PYWEBVIEW_GUI"] = "edgechromium"
        # Includes the existing timeout cleanup taskkill path. No shell is introduced.
        original = subprocess.Popen.__init__

        def no_console(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            original(self, *args, **kwargs)

        subprocess.Popen.__init__ = no_console
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def diagnose() -> dict:
    root = resource_root()
    vendor = root / "third_party/vendor"
    components = {}
    if sys.platform == "darwin":
        from backend.importer.archives import sevenzip_binary
        from backend.parsers.dwg.libredwg import discover_tools

        tools = discover_tools()
        commands = (
            ("pdfinfo", "pdfinfo", "-v"),
            ("pdftoppm", "pdftoppm", "-v"),
            ("dwg2dxf", str(tools.dwg2dxf) if tools else "dwg2dxf", "--version"),
            ("dwgread", str(tools.dwgread) if tools else "dwgread", "--version"),
            ("7zip", sevenzip_binary() or "7zz", "i"),
        )
        notice_files = ("third_party/LICENSES.md",)
    else:
        commands = (
            ("pdfinfo", str(vendor / "poppler/bin/pdfinfo.exe"), "-v"),
            ("pdftoppm", str(vendor / "poppler/bin/pdftoppm.exe"), "-v"),
            ("dwg2dxf", str(vendor / "libredwg/bin/dwg2dxf.exe"), "--version"),
            ("dwgread", str(vendor / "libredwg/bin/dwgread.exe"), "--version"),
            ("7zip", str(vendor / "7zip/7z.exe"), "i"),
        )
        notice_files = ("third_party/vendor-manifest.json", "third_party/PYTHON-LICENSE.txt")
    for name, executable, flag in commands:
        try:
            proc = subprocess.run([executable, flag], capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=15)
            text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()
            components[name] = {"ok": proc.returncode == 0 and bool(text),
                                "version": "\n".join(text.splitlines()[:4])[:600]}
        except (OSError, subprocess.TimeoutExpired) as exc:
            components[name] = {"ok": False, "error": type(exc).__name__}
    resources = {p: (root / p).is_file() for p in (
        "frontend/index.html", "backend/store/migrations/001_initial.sql",
        "backend/store/migrations/002_revisions.sql", *notice_files)}
    return {"ok": all(r["ok"] for r in components.values()) and all(resources.values()),
            "frozen": bool(getattr(sys, "frozen", False)), "components": components,
            "resources": resources}


def write_report(path: str, payload: dict) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def report_font_check() -> bool:
    """Check a real embedded CJK subset in memory; never copy system fonts into the bundle."""
    from io import BytesIO
    from backend.export.service import report_font
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas

    text = "图纸审查汇总"
    buffer = BytesIO()
    document = canvas.Canvas(buffer)
    document.setFont(report_font(), 12)
    document.drawString(30, 30, text)
    document.save()
    page = PdfReader(buffer).pages[0]
    fonts = page["/Resources"]["/Font"].get_object()
    embedded = False
    for reference in fonts.values():
        descriptor = reference.get_object().get("/FontDescriptor")
        if descriptor is not None and "/FontFile2" in descriptor.get_object():
            embedded = True
            break
    return embedded and text in page.extract_text()


def self_test() -> dict:
    """No GUI/provider calls. Data stays in a temporary directory, never the user's database."""
    report = diagnose()
    checks = {}
    platform_modules = ("webview.platforms.cocoa", "keyring.backends.macOS") if sys.platform == "darwin" else (
        "clr", "keyring.backends.Windows")
    for module in ("backend.parsers.dwg._worker", "backend.drawings._viewer_worker", "webview",
                   "openpyxl", "reportlab.pdfgen.canvas", "matplotlib.backends.backend_agg",
                   *platform_modules):
        try:
            importlib.import_module(module)
            checks[module] = True
        except Exception as exc:
            checks[module] = type(exc).__name__
    try:
        checks["embedded_cjk_report_font"] = report_font_check()
    except Exception as exc:
        checks["embedded_cjk_report_font"] = type(exc).__name__
    old_data = os.environ.get("DRAWCHECK_DATA_DIR")
    with tempfile.TemporaryDirectory(prefix="drawcheck-smoke-") as directory:
        os.environ["DRAWCHECK_DATA_DIR"] = directory
        server = None
        try:
            from backend.server import start_server
            import httpx
            token = secrets.token_urlsafe(24)
            server = start_server(token)
            with httpx.Client(base_url=server.base_url, trust_env=False) as client:
                checks["unauthenticated_401"] = client.get("/api/health").status_code == 401
                client.get("/__launch", params={"t": token})
                checks["authenticated_health"] = client.get("/api/health").json() == {"status": "ok"}
                component_response = client.get("/api/health/components")
                checks["component_endpoint"] = (component_response.status_code == 200
                                                 and component_response.json().get("available") is True)
                checks["frontend"] = client.get("/").status_code == 200
                # Actual migration/resource read, without any real credentials or model call.
                checks["settings_and_database"] = client.get("/api/settings").status_code == 200
            # Exercise the same frozen exe -> limiter -> exe dispatch chain used by DWG.
            target = Path(directory) / "child.json"
            child = subprocess.run([sys.executable, "-m", "backend.parsers.dwg._limit_exec", "1536",
                                    sys.executable, "--diagnose", str(target)], timeout=100)
            checks["frozen_worker_dispatch"] = child.returncode == 0 and target.is_file()
            if target.is_file():
                checks["frozen_worker_dispatch"] &= json.loads(target.read_text(encoding="utf-8"))["ok"]
        except Exception as exc:
            checks["backend_error"] = type(exc).__name__
        finally:
            if server is not None:
                server.stop()
                checks["backend_stopped"] = not server.thread.is_alive()
            if old_data is None:
                os.environ.pop("DRAWCHECK_DATA_DIR", None)
            else:
                os.environ["DRAWCHECK_DATA_DIR"] = old_data
    report["checks"] = checks
    report["ok"] = report["ok"] and all(value is True for value in checks.values())
    return report
