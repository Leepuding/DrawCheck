import ezdxf
from ezdxf.fonts.fonts import FontFace
import pytest

from backend.importer.diagnostics import dwg_notice
from backend.parsers.dwg import fonts, render
from backend.parsers.dwg.frames import find_frames


@pytest.mark.parametrize("missing_font", [True, False])
def test_active_font_substitution_is_reported_once(missing_font, tmp_path, monkeypatch):
    doc = ezdxf.new()
    declared = r"C:\CAD\Fonts\source.ttf" if missing_font else "source.ttf"
    for name in ("Labels", "Notes"):
        doc.styles.new(name, dxfattribs={"font": declared})
        doc.modelspace().add_text("中A", dxfattribs={"style": name})
    doc.styles.new("Unused", dxfattribs={"font": "unused.ttf"})
    monkeypatch.setattr(fonts, "exact_font_path",
                        lambda _: None if missing_font else tmp_path / "source.ttf")
    face = FontFace(filename="test-fallback.ttf")
    monkeypatch.setattr(render, "find_cjk_font", lambda: (face.filename, face))
    monkeypatch.setattr(render, "missing_glyphs",
                        lambda name, chars: set() if name == face.filename else set(chars))

    context, reasons = render._render_context(doc)
    assert context.fonts["labels"] == context.fonts["notes"] == face
    assert len(reasons) == 1 and "labels、notes" in reasons[0]
    assert "已用本机 test-fallback.ttf 替代" in reasons[0]
    if missing_font:
        assert reasons[0].count("source.ttf") == 1 and "CAD" not in reasons[0]
    else:
        assert "缺少所需中文字形" in reasons[0]
    assert "unused.ttf" not in reasons[0]
    assert "替代字体" in str(dwg_notice("；".join(reasons), (1, 1)))
    assert doc.styles.get("Labels").dxf.font == declared


def test_fallback_with_worse_glyph_coverage_is_not_assigned_or_reported(monkeypatch):
    doc = ezdxf.new()
    doc.styles.new("Labels", dxfattribs={"font": "missing.ttf"})
    doc.modelspace().add_text("中文", dxfattribs={"style": "Labels"})
    monkeypatch.setattr(fonts, "exact_font_path", lambda _: None)
    face = FontFace(filename="partial-fallback.ttf")
    monkeypatch.setattr(render, "find_cjk_font", lambda: (face.filename, face))
    monkeypatch.setattr(render, "missing_glyphs",
                        lambda name, chars: {"中", "文"} if name == face.filename else {"中"})

    context, reasons = render._render_context(doc)
    assert context.fonts.get("labels") != face
    assert any("缺字体" in reason for reason in reasons)
    assert all("已用本机" not in reason for reason in reasons)
    assert any("1 个汉字无字形" in reason for reason in reasons)


def test_undefined_reference_reports_unknown_extent_without_flagging_defined_empty_block():
    doc = ezdxf.new()
    model = doc.modelspace()
    frame = doc.blocks.new("TK-01")
    frame.add_lwpolyline([(0, 0), (420, 0), (420, 297), (0, 297)], close=True)
    model.add_blockref(frame.name, (0, 0))
    empty = doc.blocks.new("DEFINED-EMPTY")
    model.add_blockref(empty.name, (70, 80))
    missing = model.add_blockref("UNDEFINED", (800, 900))

    frames, reasons, _ = find_frames(doc)
    warnings = [row for row in reasons if "缺少块定义" in row]
    assert len(frames) == 1 and len(warnings) == 1
    assert missing.dxf.handle in warnings[0]
    assert "800,900" in warnings[0] and "受影响图框无法核实" in warnings[0]
    assert "DEFINED-EMPTY" not in warnings[0]
    assert "外部参照" not in "；".join(reasons)


def test_all_material_requests_survive_combined_historical_diagnostics():
    reason = "缺少块定义；存在外部参照；缺字体；已用本机某字体替代"
    notice = dwg_notice(reason, (2, 2))
    assert notice["summary"] == "已生成 2 张预览。"
    assert len(notice["notes"]) == 3
    assert all(text in notice["action"] for text in ("图块定义", "绑定外部参照", "制图字体", "同版 PDF"))
