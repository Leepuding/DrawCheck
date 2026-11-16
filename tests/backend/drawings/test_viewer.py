from io import BytesIO
import json

import ezdxf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from backend.drawings.router import create_router
from backend.store import Repository
from backend.store.originals import sha256_file


@pytest.fixture
def viewer(tmp_path):
    root = tmp_path / "data"
    app = FastAPI()
    app.include_router(create_router(lambda: Repository(root)))
    with Repository(root) as repo, TestClient(app) as client:
        project = repo.create_project("图纸区域")
        version = repo.create_version(project["id"], "初版")
        yield repo, client, version


def add_unit(repo, version, source, *, kind="pdf_page", ordinal=1, preview=None):
    original = repo.import_original(version["id"], source)["record"]
    metadata = {"preview_relative_path": preview.relative_to(repo.root).as_posix()} if preview else {}
    return repo.create_unit(version["id"], original["id"], kind=kind, ordinal=ordinal,
                            name=source.stem, metadata=metadata)


def preview_path(repo, version):
    return repo.root / "projects" / version["project_id"] / "cache" / "preview.png"


def route(unit):
    return f"/api/units/{unit['id']}/image-region"


def region(client, unit, box=(0, 0, 1, 1), size=(100, 100)):
    return client.get(route(unit), params=dict(zip(("x0", "y0", "x1", "y1", "width", "height"), (*box, *size))))


def as_image(response):
    assert response.status_code == 200, response.text if response.status_code != 200 else ""
    assert response.headers["content-type"] == "image/png"
    return Image.open(BytesIO(response.content))


def test_1000x_pdf_renders_original_vector_detail_and_keeps_the_original(viewer, tmp_path):
    repo, client, version = viewer
    pdf = tmp_path / "detail.pdf"
    canvas = Canvas(str(pdf), pagesize=(200, 100))
    canvas.setFillColorRGB(1, 0, 0)
    canvas.rect(100.05, 49.95, 0.06, 0.10, stroke=0, fill=1)
    canvas.save()
    preview = preview_path(repo, version)
    Image.new("RGB", (160, 80), "white").save(preview)
    unit = add_unit(repo, version, pdf, preview=preview)
    source = repo.original_path(unit["original_id"])
    before = sha256_file(source)

    info = client.get(route(unit) + "/info").json()
    assert info["kind"] == "pdf_vector"
    assert (info["width"], info["height"], info["max_zoom"]) == (160, 80, 1000)
    image = as_image(region(client, unit, (.5, .499, .501, .501), (200, 200)))
    assert image.size == (200, 200)
    assert image.getpixel((80, 100)) == (255, 0, 0)
    assert image.getpixel((30, 100)) == (255, 255, 255)
    assert sha256_file(source) == sha256_file(pdf) == before
    assert source.stat().st_mode & 0o222 == 0


def test_rotated_second_pdf_page_matches_mediabox_edges_despite_cropbox(viewer, tmp_path):
    repo, client, version = viewer
    base, pdf = tmp_path / "base.pdf", tmp_path / "rotated.pdf"
    canvas = Canvas(str(base), pagesize=(200, 100))
    canvas.translate(10, 20)
    for color, x, y in (((1, 0, 0), 0, 0), ((0, 1, 0), 100, 0),
                        ((0, 0, 1), 0, 50), ((0, 0, 0), 100, 50)):
        canvas.setFillColorRGB(*color)
        canvas.rect(x, y, 100, 50, stroke=0, fill=1)
    canvas.save()
    writer = PdfWriter()
    writer.add_blank_page(400, 400)
    page = writer.add_page(PdfReader(base).pages[0])
    page.mediabox.lower_left = (10, 20)
    page.mediabox.upper_right = (210, 120)
    page.cropbox.lower_left = (35, 35)
    page.cropbox.upper_right = (165, 90)
    page[NameObject("/UserUnit")] = NumberObject(2)
    page.rotate(90)
    writer.write(pdf)
    unit = add_unit(repo, version, pdf, ordinal=2)

    info = client.get(route(unit) + "/info").json()
    assert info["kind"] == "pdf_vector"
    assert info["width"] / info["height"] == .5
    left_top = as_image(region(client, unit, (0, 0, .1, .1), (91, 93)))
    right_bottom = as_image(region(client, unit, (.9, .9, 1, 1), (91, 93)))
    assert left_top.getpixel((45, 46)) == (255, 0, 0)
    assert right_bottom.getpixel((45, 46)) == (0, 0, 0)


def test_scan_uses_original_pixels_and_ignores_hidden_ocr_text(viewer, tmp_path):
    repo, client, version = viewer
    pdf = tmp_path / "scan.pdf"
    bitmap = Image.new("RGB", (1200, 1200), "white")
    bitmap.paste((25, 40, 210), (600, 600, 630, 630))
    canvas = Canvas(str(pdf), pagesize=(100, 100))
    canvas.drawImage(ImageReader(bitmap), 0, 0, width=100, height=100)
    hidden = canvas.beginText(20, 20)
    hidden.setTextRenderMode(3)
    hidden.textLine("OCR")
    canvas.drawText(hidden)
    canvas.save()
    preview = preview_path(repo, version)
    Image.new("RGB", (16, 16), "white").save(preview)
    unit = add_unit(repo, version, pdf, preview=preview)

    info = client.get(route(unit) + "/info").json()
    assert info["kind"] == "raster"
    assert "原文件像素限制" in info["message"]
    image = as_image(region(client, unit, (.50, .50, .55, .55)))
    assert image.getpixel((15, 15)) == (25, 40, 210)


@pytest.mark.parametrize(("box", "size"), [
    ((0, 0, 1, 1), (2049, 1)),
    ((0, 0, 1, 1), (1, 0)),
    ((float("nan"), 0, 1, 1), (100, 100)),
    ((0, 0, 1.001, 1), (100, 100)),
    ((.5, .1, .5, .9), (100, 100)),
    ((0, 0, 1e-9, 1), (100, 100)),
])
def test_invalid_region_is_rejected_before_drawing(viewer, box, size):
    _, client, _ = viewer
    response = region(client, {"id": "no-such-unit"}, box, size)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REGION"


def test_dwg_region_requires_the_exact_dxf_frame_cache_and_reuses_vector_output(viewer, tmp_path):
    repo, client, version = viewer
    source = tmp_path / "drawing.dwg"
    source.write_bytes(b"AC1015 synthetic DWG parser output fixture")
    original = repo.import_original(version["id"], source)["record"]
    cache = repo.root / "projects" / version["project_id"] / "cache" / original["id"]
    preview_relative = f"dwg/{original['sha256'][:16]}/run-existing/unit-1.png"
    preview = cache / preview_relative
    run = preview.parent
    run.mkdir(parents=True)
    Image.new("RGB", (1600, 800), "white").save(preview)
    name = "Model / 旧图框"
    (run / "result.json").write_text(json.dumps({
        "units": [{"ordinal": 1, "name": name, "preview_relative_path": preview_relative}],
        "statistics": {"source": "ezdxf", "frame_bounds": [{"ordinal": 1, "name": name, "bounds": [0, 0, 200, 100]}]},
    }), encoding="utf-8")
    dxf = run / "drawing.dxf"
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (200, 100), dxfattribs={"color": 7})
    doc.saveas(dxf)
    unit = add_unit(repo, version, source, kind="dwg_frame", preview=preview)

    info = client.get(route(unit) + "/info").json()
    assert info["kind"] == "dwg_vector"
    assert "字体替换" in info["message"]
    box = (.49, .49, .51, .51)
    image = as_image(region(client, unit, box))
    assert image.getpixel((50, 50)) == (255, 255, 255)
    assert image.getpixel((5, 5)) != (255, 255, 255)
    cached = next(run.glob("viewer-*.pdf"))
    old_mtime = cached.stat().st_mtime_ns
    as_image(region(client, unit, box, (119, 101)))
    assert cached.stat().st_mtime_ns == old_mtime

    dxf.unlink()
    assert client.get(route(unit) + "/info").json()["kind"] == "unavailable"
    assert region(client, unit, box).json()["error"]["code"] == "VIEWER_SOURCE_UNAVAILABLE"


def test_different_cold_dwg_frames_build_concurrently_and_share_same_frame(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    from backend.drawings import viewer as drawing_viewer

    prepared = []
    guarded = Lock()
    parallel = Barrier(2, timeout=4)

    def run_worker(payload, output, directory, *, timeout):
        with guarded:
            prepared.append(payload["frame"][0])
        parallel.wait()
        output.write_bytes(b"vector cache stand-in")

    monkeypatch.setattr(drawing_viewer, "_run_worker", run_worker)
    dxf = tmp_path / "drawing.dxf"
    dxf.touch()
    first = drawing_viewer.DrawingSource("dwg_vector", 1600, 900, "", dxf, 1, ("first", (0, 0, 10, 10)))
    second = drawing_viewer.DrawingSource("dwg_vector", 1600, 900, "", dxf, 2, ("second", (10, 10, 20, 20)))
    with ThreadPoolExecutor(max_workers=3) as pool:
        a = pool.submit(drawing_viewer._dwg_pdf, first)
        b = pool.submit(drawing_viewer._dwg_pdf, first)
        c = pool.submit(drawing_viewer._dwg_pdf, second)
        assert a.result() == b.result()
        assert a.result() != c.result()
    assert sorted(prepared) == ["first", "second"]
