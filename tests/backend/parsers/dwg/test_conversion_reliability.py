"""Malformed block references and independent DXF/Matplotlib font inventories."""
import sys

import ezdxf
import pytest
from PIL import Image, ImageChops

from backend.parsers.dwg.frames import Frame, find_frames, missing_block_references
from backend.parsers.dwg.libredwg import DwgOptions, run_command
from backend.parsers.dwg.render import render_frame


def test_missing_insert_retains_all_other_frames_text_and_geometry(tmp_path):
    doc = ezdxf.new("R2010")
    block = doc.blocks.new("TK-TEST")
    block.add_lwpolyline([(0, 0), (100, 0), (100, 80), (0, 80)], close=True)
    model = doc.modelspace()
    for x in (0, 200):
        model.add_blockref("TK-TEST", (x, 0))
        model.add_text(f"sheet {x}", dxfattribs={"insert": (x+5, 30), "height": 4})
        model.add_circle((x+50, 40), 20)
    frames, _, _ = find_frames(doc)
    render_frame(doc, frames[0], tmp_path / "before.png")
    # Both a directly placed bad reference and a nested bad reference must be
    # isolated. Text after the nested reference must still be extracted.
    invalid = model.add_blockref("", (20, 20))
    nested = doc.blocks.new("nested")
    nested.add_blockref("absent-definition", (0, 0))
    nested.add_text("after broken reference", dxfattribs={"insert": (5, 10), "height": 2})
    model.add_blockref("nested", (200, 0))
    before_count = len(doc.entitydb)
    repaired, reasons, counts = find_frames(doc)
    assert len(repaired) == len(frames) == 2
    assert [frame.bounds for frame in repaired] == [frame.bounds for frame in frames]
    assert any("after broken reference" == t.text for t in repaired[1].texts)
    assert counts["INSERT"] == 4
    assert len(missing_block_references(doc)) == 2
    assert sum("覆盖不完整" in row for row in reasons) == 2
    assert any(invalid.dxf.handle in row for row in reasons)
    render_frame(doc, repaired[0], tmp_path / "after.png")
    with Image.open(tmp_path / "before.png") as before, Image.open(tmp_path / "after.png") as after:
        assert before.size == after.size == (1600, 1280)
        assert ImageChops.difference(before.convert("RGB"), after.convert("RGB")).getbbox() is None
    assert len(doc.entitydb) == before_count  # No geometry deleted, repaired, or fabricated.
    assert invalid.dxf.name == ""


def test_missing_array_block_keeps_each_visible_attribute(tmp_path):
    def render_array(name, destination):
        doc = ezdxf.new("R2010")
        doc.blocks.new("empty")
        model = doc.modelspace()
        insert = model.add_blockref(name, (5, 5), dxfattribs={
            "row_count": 2, "column_count": 2, "row_spacing": 30, "column_spacing": 35,
        })
        insert.add_attrib("LABEL", "Visible", (5, 5), dxfattribs={"height": 4})
        render_frame(doc, Frame(model, "test", (0, 0, 90, 75), []), destination)

    expected, actual = tmp_path / "array.png", tmp_path / "broken.png"
    render_array("empty", expected)
    render_array("", actual)
    with Image.open(expected) as before, Image.open(actual) as after:
        assert ImageChops.difference(before.convert("RGB"), after.convert("RGB")).getbbox() is None


def test_empty_matplotlib_cache_does_not_probe_system_fonts(tmp_path):
    # Run a fresh worker so this verifies import-time FontManager construction,
    # independent of any font cache or imports in the pytest process.
    code = '''
import subprocess
original = subprocess.check_output
def guarded(command, *args, **kwargs):
    if command[0] in ("system_profiler", "fc-list"):
        raise RuntimeError("Matplotlib attempted unnecessary system font enumeration")
    return original(command, *args, **kwargs)
subprocess.check_output = guarded
import matplotlib.font_manager
from ezdxf.fonts import fonts
from backend.parsers.dwg.render import find_cjk_font, missing_glyphs
assert matplotlib.font_manager.fontManager.ttflist
font = find_cjk_font()
if font:
    assert not missing_glyphs(font[0], "中文图纸")
print("independent font inventories ready")
'''
    result = run_command([sys.executable, "-c", code], tmp_path, "font-inventory",
                         DwgOptions(timeout_sec=45))
    assert result.returncode == 0, result.stderr_tail
    assert not result.reason
    assert "independent font inventories ready" in (tmp_path / "font-inventory.stdout.log").read_text()


def test_cjk_text_reaches_matplotlib_as_ezdxf_glyph_paths(tmp_path, monkeypatch):
    from ezdxf.addons.drawing.recorder import FilledPathsRecord
    from backend.parsers.dwg import render
    from backend.parsers.dwg.frames import Frame
    import matplotlib.text

    font = render.find_cjk_font()
    if font is None:
        pytest.skip("No installed CJK font; existing renderer reports missing glyphs")
    assert not render.missing_glyphs(font[0], "中文图纸")
    doc = ezdxf.new("R2010")
    doc.styles.get("Standard").dxf.font = font[0]
    model = doc.modelspace()
    model.add_text("中文图纸", dxfattribs={"insert": (5, 10), "height": 10})
    original_init = matplotlib.text.Text.__init__
    def text_init(self, *args, **kwargs):
        text = kwargs.get("text", args[2] if len(args) > 2 else "")
        assert not any(render._is_cjk(ch) for ch in str(text)), "CJK was delegated to Matplotlib fonts"
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(matplotlib.text.Text, "__init__", text_init)
    reasons = render_frame(doc, Frame(model, "CJK", (0, 0, 60, 25), []), tmp_path / "cjk.png")
    assert not any("中文字形无法渲染" in row for row in reasons)
    recording = next(iter(render._RECORDINGS[doc].values()))
    paths = [path for record in recording.player.records if isinstance(record, FilledPathsRecord)
             for path in record.paths]
    assert len(paths) >= 4  # The four CJK glyph outlines already exist before PNG drawing.
    assert all(len(path) > 0 for path in paths)


def test_conversion_warnings_are_not_limited_to_stderr_tail(tmp_path):
    from backend.parsers.dwg._worker import conversion_warning_counts
    log = tmp_path / "conversion.stderr.log"
    log.write_text("Warning: Skip HATCH common handles due to short handle stream\n" * 3
                   + "information only\n" * 2000 + "Warning: Unstable Class object\n")
    counts = conversion_warning_counts(log)
    assert counts["Warning: Skip HATCH common handles due to short handle stream"] == 3
    assert counts["Warning: Unstable Class object"] == 1
    assert sum(counts.values()) == 4


def test_worker_default_allows_full_size_large_drawings(monkeypatch):
    monkeypatch.delenv("DRAWCHECK_DWG_WORKER_TIMEOUT_SEC", raising=False)
    assert DwgOptions().worker_timeout_sec == 1200
    assert DwgOptions.from_env().worker_timeout_sec >= 1200
    monkeypatch.setenv("DRAWCHECK_DWG_WORKER_TIMEOUT_SEC", "42")
    assert DwgOptions.from_env().worker_timeout_sec == 42


def test_internal_block_alias_is_not_reported_as_external_dwg_dependency():
    doc = ezdxf.new("R2010")
    alias = doc.blocks.new("internal-alias")
    alias.block.dxf.flags = 1
    alias.block.dxf.xref_path = "*A4"
    alias.add_circle((0, 0), 10)
    doc.modelspace().add_blockref("internal-alias", (0, 0))
    _, reasons, _ = find_frames(doc)
    assert not any("外部参照" in row for row in reasons)
    xref = doc.blocks.new("real-xref")
    xref.block.dxf.flags = 4
    xref.block.dxf.xref_path = "not-supplied.dwg"
    _, reasons, _ = find_frames(doc)
    assert any("外部参照" in row for row in reasons)
