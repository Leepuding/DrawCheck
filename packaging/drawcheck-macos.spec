"""Native macOS application. The drawing CLI tools are installed separately."""
from pathlib import Path
import os
import platform
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

if sys.platform != "darwin" or platform.machine() != "arm64":
    raise SystemExit("Build on Apple Silicon using an arm64 Python interpreter.")

packaging = Path(SPECPATH)
root = packaging.parent
datas = [
    (str(root / "frontend"), "frontend"),
    (str(root / "backend/store/migrations"), "backend/store/migrations"),
    (str(root / "LICENSES.md"), "third_party"),
    (str(root / "docs/third-party"), "third_party/license-texts"),
]
binaries = []
hiddenimports = [
    "webview.platforms.cocoa", "keyring.backends.macOS", "keyring.backends.fail",
    "matplotlib.backends.backend_agg",
]
for package in ("rapidocr", "pypdfium2", "pypdfium2_raw"):
    resources, libraries, modules = collect_all(package)
    datas += resources
    binaries += libraries
    hiddenimports += modules

hiddenimports += collect_submodules("uvicorn")
for distribution in ("pypdf", "ezdxf", "pywebview", "openpyxl", "rapidocr", "onnxruntime",
                     "pdfplumber", "keyring"):
    datas += copy_metadata(distribution)
analysis = Analysis(
    [str(packaging / "bundle_entry.py")],
    pathex=[str(root), str(packaging)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + [
        "backend.parsers.dwg._worker", "backend.drawings._viewer_worker",
    ],
    hooksconfig={"matplotlib": {"backends": ["Agg"]}},
    excludes=["pytest", "playwright", "IPython", "tkinter", "PyQt5", "PyQt6",
              "PySide2", "PySide6", "webview.platforms.qt", "webview.platforms.gtk"],
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive, analysis.scripts, [], exclude_binaries=True, name="DrawCheck",
    console=False, target_arch="arm64", upx=False,
)
directory = COLLECT(executable, analysis.binaries, analysis.datas, name="DrawCheck", upx=False)
app = BUNDLE(
    directory, name="DrawCheck.app", bundle_identifier="com.drawcheck.desktop",
    version=os.environ["DRAWCHECK_BUILD_VERSION"],
    info_plist={"CFBundleDisplayName": "图纸审查"},
)
