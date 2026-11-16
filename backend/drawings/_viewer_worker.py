"""Isolate native PDF parsing and DWG vector preparation from the HTTP process."""
from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path
import sys
from threading import Event, Thread


def _windows_peak_memory():
    from ctypes import wintypes

    class MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
            )
        ]

    current_process = ctypes.windll.kernel32.GetCurrentProcess
    current_process.restype = wintypes.HANDLE
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = (wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD)
    counters = MemoryCounters(cb=ctypes.sizeof(MemoryCounters))
    if not get_memory(current_process(), ctypes.byref(counters), counters.cb):
        raise OSError("不能读取绘图子进程内存")
    return max(counters.PeakWorkingSetSize, counters.PeakPagefileUsage)


def _memory_guard(directory, limit_bytes):
    """Linux enforces RLIMIT_AS in the launcher; other platforms check peak memory."""
    if sys.platform == "darwin":
        import resource

        def peak():
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    elif sys.platform == "win32":
        peak = _windows_peak_memory
    else:
        return lambda: None

    stop = Event()

    def check():
        if peak() > limit_bytes:
            (directory / "memory-limit").touch()
            os._exit(86)

    def watch():
        while not stop.wait(0.05):
            check()

    def finish():
        check()
        stop.set()

    check()
    Thread(target=watch, daemon=True).start()
    return finish


def _media_page(source, ordinal, document):
    import pypdfium2.raw as pdfium

    page = document[ordinal - 1]
    media = page.get_mediabox(fallback_ok=False)
    if media is None:
        # PDFium's direct box getter does not resolve inherited MediaBox entries.
        from pypdf import PdfReader

        with source.open("rb") as stream:
            media = tuple(float(value) for value in PdfReader(stream).pages[ordinal - 1].mediabox)
    if not (all(math.isfinite(value) for value in media) and media[0] < media[2] and media[1] < media[3]):
        raise ValueError("无效的 PDF 页面尺寸")
    # Existing previews use MediaBox. Change the crop only in the worker's in-memory page.
    pdfium.FPDFPage_SetCropBox(page, *media)
    return page


def _contents(page):
    import pypdfium2.raw as pdfium

    vector = raster = False
    media = page.get_bbox()
    get_active = getattr(pdfium, "FPDFPageObj_GetIsActive", None)
    for obj in page.get_objects():
        active = ctypes.c_int(1)
        if get_active and get_active(obj, active) and not active.value:
            continue
        if obj.type == pdfium.FPDF_PAGEOBJ_IMAGE:
            raster = True
        elif obj.type == pdfium.FPDF_PAGEOBJ_SHADING:
            vector = True
        elif obj.type == pdfium.FPDF_PAGEOBJ_TEXT:
            mode = pdfium.FPDFTextObj_GetTextRenderMode(obj)
            vector |= mode not in {pdfium.FPDF_TEXTRENDERMODE_INVISIBLE, pdfium.FPDF_TEXTRENDERMODE_CLIP,
                                   pdfium.FPDF_TEXTRENDERMODE_UNKNOWN}
        elif obj.type == pdfium.FPDF_PAGEOBJ_PATH:
            fill, stroke = ctypes.c_int(), ctypes.c_int()
            if pdfium.FPDFPath_GetDrawMode(obj, fill, stroke):
                # A scan can include a page-sized solid backing rectangle. It adds no inspectable geometry.
                background = (not obj.level and not stroke.value
                              and all(math.isclose(a, b, abs_tol=0.01) for a, b in zip(obj.get_bounds(), media)))
                vector |= bool(stroke.value or (fill.value and not background))
        if vector and raster:
            break
    return vector, raster


def _prepare_dwg(payload, source, directory):
    from backend.parsers.dwg.convert import load_complete_dxf
    from backend.parsers.dwg.frames import Frame
    from backend.parsers.dwg.render import render_frame

    frame_name, bounds = payload["frame"]
    doc = load_complete_dxf(source)
    layouts = [layout for layout in doc.layouts if frame_name.startswith(layout.name + " / ")]
    if not layouts:
        raise ValueError("转换缓存未找到对应图框布局")
    # Historical import results encode the layout in the prefix. A layout name may itself contain ' / '.
    layout = max(layouts, key=lambda candidate: len(candidate.name))
    frame = Frame(layout, frame_name, tuple(bounds), [])
    from pypdfium2 import PdfDocument

    output = directory / "sheet.pdf"
    render_frame(doc, frame, output)
    with PdfDocument(output) as document:
        page = document[0]
        try:
            has_vector, _ = _contents(page)
        finally:
            page.close()
    if not has_vector:
        (directory / "no-vector").touch()



def _run(payload, directory):
    source, operation = Path(payload["source"]), payload["operation"]
    if operation == "dwg":
        _prepare_dwg(payload, source, directory)
        return
    from pypdfium2 import PdfBitmap, PdfDocument
    import pypdfium2.raw as pdfium

    with PdfDocument(source) as document:
        page = _media_page(source, payload["ordinal"], document)
        try:
            if operation == "inspect":
                width, height = page.get_size()
                vector, raster = _contents(page)
                (directory / "info.json").write_text(json.dumps(
                    {"width": width, "height": height, "vector": vector, "raster": raster}, allow_nan=False),
                    encoding="utf-8")
            elif operation == "region":
                region = payload["region"]
                width, height = region["width"], region["height"]
                bitmap = PdfBitmap.new_native(width, height, pdfium.FPDFBitmap_BGR)
                try:
                    bitmap.fill_rect((255, 255, 255, 255), 0, 0, width, height)
                    pdfium.FPDF_RenderPageBitmap(bitmap, page, -region["left"], -region["top"],
                                                 region["full_width"], region["full_height"], 0,
                                                 pdfium.FPDF_ANNOT)
                    bitmap.to_pil().save(directory / "region.png", compress_level=2)
                finally:
                    bitmap.close()
            else:
                raise ValueError("未知的高清绘图操作")
        finally:
            page.close()


def main():
    request = Path(sys.argv[1])
    payload = json.loads(request.read_text(encoding="utf-8"))
    # An already-running development server may still send the first request format.
    budget_mb = payload.get("memory_mb", 1536 if payload["operation"] == "dwg" else 512)
    finish = _memory_guard(request.parent, budget_mb * 1024 * 1024)
    try:
        _run(payload, request.parent)
    finally:
        finish()


if __name__ == "__main__":
    main()
