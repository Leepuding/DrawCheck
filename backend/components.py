"""Read-only component diagnostics, no paths or environment values in responses."""
import importlib.metadata
import re
import shutil
import subprocess

def components():
    items=[]
    for package in ('pypdf','ezdxf','pywebview','openpyxl','rapidocr','onnxruntime','pdfplumber'):
        try: version=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: version=None
        items.append({'name':package,'available':version is not None,'version':version})
    for label,names,arg in [('PDF预览',('pdftoppm',),'-v'),('PDF读取',('pdfinfo',),'-v'),
                           ('DWG转换',('dwg2dxf',),'--version'),('DWG读取',('dwgread',),'--version'),('压缩包',('7z','7zz','7za'),'i')]:
        path=next((p for n in names if (p:=shutil.which(n))),None)
        version=None; available=False
        if path:
            try:
                response=subprocess.run([path,arg],capture_output=True,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                match=re.search(r'\b\d+\.\d+(?:\.\d+)?\b',(response.stdout+response.stderr).decode('utf-8',errors='replace'))
                available=response.returncode==0
                version=match.group(0) if match else None
            except (OSError,subprocess.TimeoutExpired): pass
        items.append({'name':label,'available':available,'version':version})
    return {'items':items,'available':all(x['available'] for x in items)}
