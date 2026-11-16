"""Offline build inputs and local prerequisites. Never downloads or reads model keys."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata as metadata
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_MODULES = ("fastapi", "uvicorn", "webview", "httpx", "pypdf", "ezdxf",
                    "matplotlib", "reportlab", "keyring", "multipart", "openpyxl",
                    "rapidocr", "onnxruntime", "pdfplumber")
TOOLS = {"pdfinfo": ("pdfinfo", "-v"), "pdftoppm": ("pdftoppm", "-v"),
         "dwg2dxf": ("dwg2dxf", "--version"), "dwgread": ("dwgread", "--version"),
         "7zip": ("7z", "i")}
VENDOR_EXECUTABLES = {"pdfinfo": "poppler/bin/pdfinfo.exe",
                      "pdftoppm": "poppler/bin/pdftoppm.exe",
                      "dwg2dxf": "libredwg/bin/dwg2dxf.exe",
                      "dwgread": "libredwg/bin/dwgread.exe", "7zip": "7zip/7z.exe"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def contained(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if (not relative or rel.is_absolute() or ".." in rel.parts or "\\" in relative
            or ":" in relative or str(rel) != relative):
        raise ValueError(f"Unsafe vendor path: {relative}")
    result = root.joinpath(*rel.parts)
    if any(p.is_symlink() for p in (root, result, *result.parents)):
        raise ValueError(f"Symlink is not allowed: {relative}")
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Vendor path escapes root: {relative}")
    return result


def run_version(path: str, argument: str) -> dict:
    try:
        result = subprocess.run([path, argument], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
        return {"ok": result.returncode == 0 and bool(lines), "returncode": result.returncode,
                "version": "\n".join(lines.splitlines()[:4])[:600]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": type(exc).__name__}


def local_report() -> dict:
    modules = {name: importlib.util.find_spec(name) is not None for name in REQUIRED_MODULES}
    tools = {}
    for name, (executable, argument) in TOOLS.items():
        if name in ("dwg2dxf", "dwgread") and "DRAWCHECK_LIBREDWG_BIN" in os.environ:
            path = shutil.which(executable, path=os.environ["DRAWCHECK_LIBREDWG_BIN"])
        elif name == "7zip":
            configured = os.environ.get("DRAWCHECK_7Z", "").strip()
            path = (shutil.which(configured) if configured else None) if configured else (
                shutil.which("7z") or shutil.which("7za") or shutil.which("7zz"))
        else:
            path = shutil.which(executable)
        tools[name] = {"path": path, **(run_version(path, argument) if path else {"ok": False})}
    assets = all((ROOT / path).is_file() for path in
                 ("frontend/index.html", "backend/store/migrations/001_initial.sql"))
    return {"ok": all(modules.values()) and all(t["ok"] for t in tools.values()) and assets,
            "platform": sys.platform, "python": sys.version.split()[0], "modules": modules,
            "tools": tools, "assets": assets,
            "note": "Checks discovery/version only; does not prove GUI or drawing correctness."}


def environment() -> dict[str, str]:
    return dict(sorted((re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower(), d.version)
                       for d in metadata.distributions() if d.metadata.get("Name")))


def environment_lock(path: Path, verify: bool = False):
    current = environment()
    if verify:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s;]+", line) for line in lines):
            raise ValueError("Environment lock must contain exact name==version pins only")
        expected = dict(line.split("==", 1) for line in lines)
        if current != expected:
            raise ValueError("Environment drift: recreate the offline venv from requirements-win.lock")
    else:
        if sys.platform != "win32" or struct.calcsize("P") != 8:
            raise ValueError("Create the Windows lock on Windows x64, not from a Mac environment")
        path.write_text("# Windows build environment; retain matching offline wheels and their hashes.\n"
                        + "\n".join(f"{k}=={v}" for k, v in current.items()) + "\n", encoding="utf-8")


def windows_environment() -> dict:
    if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.version_info[:2] != (3, 12):
        raise ValueError("Use Windows x64 Python 3.12 for the packaging baseline")
    for name in (*REQUIRED_MODULES, "PyInstaller", "clr", "keyring.backends.Windows"):
        importlib.import_module(name)
    if int(metadata.version("pyinstaller").split(".")[0]) != 6:
        raise ValueError("PyInstaller 6 is required")
    return {"ok": True, "python": sys.version.split()[0], "platform": "win-amd64"}


def check_pe(path: Path):
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError(f"Not a Windows executable: {path.name}")
        stream.seek(0x3C)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0" or stream.read(2) != b"\x64\x86":
            raise ValueError(f"Not an AMD64 PE executable: {path.name}")


def validate_vendor(manifest_path: Path, vendor: Path, execute: bool = False) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or manifest.get("platform") != "win-amd64":
        raise ValueError("Expected schema=1, platform=win-amd64")
    components = manifest["components"]
    ids = [c["id"] for c in components]
    if len(ids) != len(set(ids)) or not {"poppler", "libredwg", "7zip", "webview2", "fonts"} <= set(ids):
        raise ValueError("Missing or duplicate required vendor components")
    listed = {}
    for component in components:
        for key in ("version", "origin", "source_notes"):
            value = component.get(key, "")
            if not value or "REPLACE" in value:
                raise ValueError(f"Complete {component['id']}.{key} before building")
        if not component.get("licenses"):
            raise ValueError(f"Missing full license texts: {component['id']}")
        if component["id"] in ("poppler", "libredwg", "7zip") and not component.get("sources"):
            raise ValueError(f"Missing corresponding source material: {component['id']}")
        for path in component["licenses"] + component.get("sources", []):
            if path not in component["files"]:
                raise ValueError(f"Unhashed license/source file: {path}")
        for relative, expected in component["files"].items():
            if relative in listed or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError(f"Duplicate path or invalid SHA256: {relative}")
            path = contained(vendor, relative)
            if not path.is_file() or path.stat().st_size == 0 or sha256(path) != expected:
                raise ValueError(f"Missing, empty or changed vendor input: {relative}")
            listed[relative] = expected
    actual = {p.relative_to(vendor).as_posix() for p in vendor.rglob("*") if p.is_file()}
    if actual != set(listed):
        raise ValueError(f"Unlisted vendor inputs: {sorted(actual - set(listed))}")
    required = [*VENDOR_EXECUTABLES.values(), "webview2/MicrosoftEdgeWebView2RuntimeInstallerX64.exe"]
    if not set(required) <= set(listed):
        raise ValueError("Missing required executable paths; use vendor.example.json layout")
    for relative in VENDOR_EXECUTABLES.values():
        check_pe(contained(vendor, relative))
    versions = {}
    if execute:
        if sys.platform != "win32":
            raise ValueError("Windows vendor executables can only be smoke-tested on Windows")
        for name, relative in VENDOR_EXECUTABLES.items():
            result = run_version(str(contained(vendor, relative)), TOOLS[name][1])
            if not result["ok"]:
                raise ValueError(f"Vendor executable smoke failed: {name}: {result}")
            component_id = "7zip" if name == "7zip" else relative.split("/")[0]
            declared = next(c["version"] for c in components if c["id"] == component_id)
            if not re.search(r"(?<![\w.])" + re.escape(declared) + r"(?![\w.])", result["version"]):
                raise ValueError(f"Declared version does not match CLI output: {name}: {declared}")
            versions[name] = result["version"]
    return {"ok": True, "manifest": manifest, "files": listed, "versions": versions}


def collect_licenses(output: Path, vendor: Path):
    """Copy originals from installed distributions; fail if a full text is absent."""
    output.mkdir(parents=True, exist_ok=True)
    records, missing = [], []
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata.get("Name", "")):
        name = dist.metadata["Name"]
        safe_name = re.sub(r"[-_.]+", "-", name).lower()
        dest = output / "python" / safe_name
        dest.mkdir(parents=True, exist_ok=True)
        texts = []
        for item in dist.files or ():
            if any(re.match(r"(?i)^(license|licence|copying|copyright|notice)([._-].*)?$", p)
                   for p in Path(str(item)).parts):
                source = Path(dist.locate_file(item))
                if source.is_file():
                    target = dest / f"{len(texts):03d}-{source.name}"
                    shutil.copy2(source, target)
                    texts.append(target.relative_to(output).as_posix())
        supplemental = vendor / "python-licenses" / safe_name
        if supplemental.is_dir():
            for source in sorted(supplemental.rglob("*")):
                if source.is_file():
                    target = dest / f"extra-{len(texts):03d}-{source.name}"
                    shutil.copy2(source, target)
                    texts.append(target.relative_to(output).as_posix())
        # Some legacy sdists put the entire license only in METADATA.
        license_text = dist.metadata.get("License", "")
        if not texts and len(license_text) > 250 and "redistribution" in license_text.lower():
            target = dest / "LICENSE-from-METADATA.txt"
            target.write_text(license_text, encoding="utf-8")
            texts.append(target.relative_to(output).as_posix())
        if not texts:
            missing.append(name)
        records.append({"name": name, "version": dist.version, "license_files": texts})
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        python_license = vendor / "python-licenses" / "python" / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, output / "PYTHON-LICENSE.txt")
    else:
        missing.append("Python runtime (LICENSE.txt)")
    (output / "python-components.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    if missing:
        raise ValueError("Supply full texts under vendor/python-licenses and hash them in the manifest: "
                         + ", ".join(missing))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    local = sub.add_parser("local")
    local.add_argument("--output", type=Path)
    sub.add_parser("windows")
    lock = sub.add_parser("lock")
    lock.add_argument("--output", type=Path, required=True)
    lock.add_argument("--verify", action="store_true")
    vendor = sub.add_parser("vendor")
    vendor.add_argument("--manifest", type=Path, required=True)
    vendor.add_argument("--vendor", type=Path, required=True)
    vendor.add_argument("--execute", action="store_true")
    vendor.add_argument("--output", type=Path)
    licenses = sub.add_parser("licenses")
    licenses.add_argument("--output", type=Path, required=True)
    licenses.add_argument("--vendor", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "local":
            result = local_report()
        elif args.action == "windows":
            result = windows_environment()
        elif args.action == "lock":
            environment_lock(args.output, args.verify)
            return 0
        elif args.action == "vendor":
            result = validate_vendor(args.manifest, args.vendor, args.execute)
        else:
            collect_licenses(args.output, args.vendor)
            return 0
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if getattr(args, "output", None):
            args.output.write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return 0 if result["ok"] else 1
    except (OSError, ValueError, KeyError, TypeError, ImportError, struct.error) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
