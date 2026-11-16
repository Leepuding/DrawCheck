"""Portable checks for packaging boundaries; these are not Windows installation acceptance."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import struct
import subprocess
import sys

import pytest

PACK = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("drawcheck_packaging_preflight", PACK / "preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def pe_bytes():
    header = bytearray(0x100)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x80)
    header[0x80:0x86] = b"PE\0\0\x64\x86"
    return header


def vendor_fixture(tmp_path):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    components = []
    for name in ("poppler", "libredwg", "7zip", "webview2", "fonts"):
        files = {f"{name}/LICENSE.txt": b"Complete test license text"}
        sources = []
        if name in ("poppler", "libredwg", "7zip"):
            sources = [f"sources/{name}.tar.xz"]
            files[sources[0]] = b"Test corresponding source archive"
        for relative in preflight.VENDOR_EXECUTABLES.values():
            if relative.startswith(name + "/"):
                files[relative] = pe_bytes()
        if name == "webview2":
            files["webview2/MicrosoftEdgeWebView2RuntimeInstallerX64.exe"] = pe_bytes()
        hashes = {}
        for relative, content in files.items():
            path = vendor / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            hashes[relative] = hashlib.sha256(content).hexdigest()
        components.append({"id": name, "version": "1.0", "origin": "https://example.invalid/upstream",
                           "source_notes": "Fixture; not a distributable artifact", "files": hashes,
                           "licenses": [f"{name}/LICENSE.txt"], "sources": sources})
    manifest = tmp_path / "vendor.json"
    manifest.write_text(json.dumps({"schema": 1, "platform": "win-amd64", "components": components}))
    return vendor, manifest


def test_vendor_inventory_tamper_and_extra_files(tmp_path):
    vendor, manifest = vendor_fixture(tmp_path)
    assert preflight.validate_vendor(manifest, vendor)["ok"]
    executable = vendor / "poppler/bin/pdfinfo.exe"
    old = executable.read_bytes()
    executable.write_bytes(old + b"changed")
    with pytest.raises(ValueError, match="changed vendor input"):
        preflight.validate_vendor(manifest, vendor)
    executable.write_bytes(old)
    (vendor / "unapproved.dll").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="Unlisted vendor inputs"):
        preflight.validate_vendor(manifest, vendor)


@pytest.mark.parametrize("relative", ["../secret", "/etc/passwd", "a/../../x", "C:/secret",
                                      "safe\\..\\secret", "a/./b", ""])
def test_vendor_path_cannot_escape(tmp_path, relative):
    with pytest.raises(ValueError):
        preflight.contained(tmp_path, relative)


def test_vendor_symlink_rejected(tmp_path):
    (tmp_path / "real").write_text("not a vendor input")
    try:
        (tmp_path / "linked").symlink_to(tmp_path / "real")
    except OSError:
        pytest.skip("Platform does not permit symlink creation")
    with pytest.raises(ValueError, match="Symlink"):
        preflight.contained(tmp_path, "linked")


def test_vendor_sources_required(tmp_path):
    vendor, manifest = vendor_fixture(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["components"][0]["sources"] = []
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="corresponding source"):
        preflight.validate_vendor(manifest, vendor)


def test_invalid_pe_cannot_pass_with_correct_hash(tmp_path):
    vendor, manifest = vendor_fixture(tmp_path)
    payload = json.loads(manifest.read_text())
    path = "poppler/bin/pdfinfo.exe"
    (vendor / path).write_bytes(b"not an exe")
    payload["components"][0]["files"][path] = preflight.sha256(vendor / path)
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Not a Windows executable"):
        preflight.validate_vendor(manifest, vendor)


def test_environment_drift_blocks_build(tmp_path, monkeypatch):
    lock = tmp_path / "environment.lock"
    lock.write_text("some-package==1.2.3\n")
    monkeypatch.setattr(preflight, "environment", lambda: {"some-package": "1.2.3"})
    preflight.environment_lock(lock, verify=True)
    lock.write_text("some-package==1.2.4\n")
    with pytest.raises(ValueError, match="drift"):
        preflight.environment_lock(lock, verify=True)


def test_vendor_version_must_match_manifest(tmp_path, monkeypatch):
    vendor, manifest = vendor_fixture(tmp_path)
    monkeypatch.setattr(preflight.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "run_version", lambda *args: {"ok": True, "version": "tool 9.9"})
    with pytest.raises(ValueError, match="Declared version"):
        preflight.validate_vendor(manifest, vendor, execute=True)


def test_short_license_identifier_cannot_replace_full_text(tmp_path, monkeypatch):
    class Distribution:
        metadata = {"Name": "test-legacy-package", "License": "MIT"}
        files = []
        version = "1.0"

    monkeypatch.setattr(preflight.metadata, "distributions", lambda: [Distribution()])
    with pytest.raises(ValueError, match="test-legacy-package"):
        preflight.collect_licenses(tmp_path / "notices", tmp_path / "vendor")


def test_worker_dispatch_runs_command_without_launching_ui(tmp_path):
    output = tmp_path / "child output.json"
    command = [sys.executable, str(PACK / "bundle_entry.py"), "-m",
               "backend.parsers.dwg._limit_exec", "1536", sys.executable, "-c",
               "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('child ran')", str(output)]
    result = subprocess.run(command, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert output.read_text() == "child ran"


@pytest.mark.parametrize("argv", [["--browser"], ["-m", "http.server"], ["-m"],
                                  ["-m", "backend.parsers.dwg._limit_exec", "1"],
                                  ["-m", "backend.parsers.dwg._limit_exec", "bad", "command"]])
def test_product_entry_rejects_browser_and_other_modules(argv):
    result = subprocess.run([sys.executable, str(PACK / "bundle_entry.py"), *argv], timeout=10)
    assert result.returncode == 2


def test_integrated_components_route_preserves_auth_without_returning_paths():
    from fastapi.testclient import TestClient
    from backend.app import create_app

    client = TestClient(create_app("packaging-test-token"))
    assert client.get("/api/health/components").status_code == 401
    response = client.get("/api/health/components", headers={"Authorization": "Bearer packaging-test-token"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "available"}
    assert isinstance(body["items"], list)
    assert isinstance(body["available"], bool)
    components = body["items"]
    assert {"pypdf", "ezdxf", "pywebview", "openpyxl"} <= {item["name"] for item in components}
    assert len(components) >= 9
    for component in components:
        assert set(component) == {"name", "available", "version"}
        assert isinstance(component["name"], str)
        assert isinstance(component["available"], bool)
        assert component["version"] is None or re.fullmatch(r"\d+(?:\.\d+){1,3}", component["version"])
    assert body["available"] == all(item["available"] for item in components)
