# Windows x64 only. Invoked by build.ps1 after offline inputs pass preflight.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

if sys.platform != 'win32':
    raise SystemExit('Build this spec on Windows x64; Mac cannot cross-compile a Windows exe.')
root = Path(SPECPATH).parent
stage = Path(SPECPATH) / 'stage'
datas = [(str(root / 'frontend'), 'frontend'),
         (str(root / 'backend/store/migrations'), 'backend/store/migrations'),
         (str(stage / 'third_party'), 'third_party')]
binaries, hiddenimports = [], []
# pywebview includes .NET/WebView2 interop DLLs. Its hook plus collect_all retains them.
for package in ('webview', 'clr_loader', 'pythonnet', 'rapidocr', 'pypdfium2', 'pypdfium2_raw'):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules('backend')
hiddenimports += collect_submodules('uvicorn')
hiddenimports += ['keyring.backends.Windows', 'keyring.backends.fail', 'clr',
                  'matplotlib.backends.backend_agg']
datas += copy_metadata('keyring', recursive=True)
for distribution in ('pypdf', 'ezdxf', 'pywebview', 'openpyxl', 'rapidocr', 'onnxruntime', 'pdfplumber'):
    datas += copy_metadata(distribution)
a = Analysis([str(Path(SPECPATH) / 'bundle_entry.py')],
             pathex=[str(root), str(Path(SPECPATH))], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hooksconfig={'matplotlib': {'backends': ['Agg']}},
             excludes=['pytest', 'playwright', 'IPython', 'tkinter', 'PyQt5', 'PyQt6',
                       'PySide2', 'PySide6', 'webview.platforms.cef'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='DrawCheck',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='DrawCheck')
