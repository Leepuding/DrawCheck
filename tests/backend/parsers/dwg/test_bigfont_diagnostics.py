"""Format evidence, not customer font names, controls BIGFONT limitations."""
from types import SimpleNamespace

import ezdxf
import pytest

from backend.parsers.dwg import render
from backend.parsers.dwg.frames import Frame
from backend.parsers.dwg.libredwg import DwgOptions
from backend.parsers.dwg.parser import parse_dwg_file
from backend.parsers.dwg.status import DwgFileStatus


def text_document(*, font="missing-normal.shx", bigfont=""):
    doc = ezdxf.new("R2010")
    style = doc.styles.get("Standard")
    style.dxf.font = font
    style.dxf.bigfont = bigfont
    doc.modelspace().add_text("text", dxfattribs={"insert": (10, 10), "height": 5})
    return doc


def test_style_bigfont_slot_explicitly_reports_engine_limitation_without_file():
    doc = text_document(bigfont="unavailable-arbitrary-name.shx")
    rows = render.bigfont_diagnostics(doc)
    assert len(rows) == 1
    assert rows[0]["code"] == "FONT_FORMAT_UNSUPPORTED_BIGFONT"
    assert rows[0]["evidence"] == "STYLE.bigfont"
    assert rows[0]["font"] == "unavailable-arbitrary-name.shx"
    assert "不受当前 ezdxf 渲染链支持" in rows[0]["reason"]
    assert "补齐字体文件仍不能证明" in rows[0]["reason"]


def test_unused_style_and_missing_shx_name_do_not_establish_bigfont_format():
    doc = text_document(font="named-BIGFONT-but-unverified.shx")
    doc.styles.new("unused", dxfattribs={"bigfont": "declared-but-unused.shx"})
    assert render.bigfont_diagnostics(doc) == []


@pytest.mark.parametrize("suffix,header", [
    (".shx", b"AutoCAD-86 bigfont 1.0\r\n\x1a"),
    (".shp", b"; synthetic font header\n*BIGFONT 6,0,0\n"),
])
def test_actual_font_header_confirms_format_in_primary_font_slot(tmp_path, suffix, header):
    font = tmp_path / f"arbitrary-font-name{suffix}"
    font.write_bytes(header)
    rows = render.bigfont_diagnostics(text_document(font=str(font)))
    assert len(rows) == 1 and rows[0]["evidence"] == "font_header"
    assert rows[0]["font"] == font.name
    assert str(tmp_path) not in rows[0]["reason"]


def test_normal_shape_font_header_is_not_misclassified_by_filename(tmp_path):
    font = tmp_path / "BIGFONT-in-name-only.shx"
    font.write_bytes(b"AutoCAD-86 unifont 1.0\r\n\x1a")
    assert render.bigfont_diagnostics(text_document(font=str(font))) == []


def test_exact_registered_path_can_confirm_header_without_using_fallback(tmp_path, monkeypatch):
    from ezdxf.fonts import fonts

    actual = tmp_path / "registered-header.shx"
    actual.write_bytes(b"AutoCAD-86 bigfont 1.0\r\n\x1a")
    lookups = []
    class Cache:
        def get(self, name, fallback):
            lookups.append((name, fallback))
            assert name == fallback == "registered-alias.shx"
            return SimpleNamespace(file_path=actual)
    monkeypatch.setattr(fonts, "font_manager", SimpleNamespace(
        has_font=lambda name: name == "registered-alias.shx", _font_cache=Cache()))
    assert render.bigfont_diagnostics(text_document(font="registered-alias.shx"))[0]["evidence"] == "font_header"
    assert render.bigfont_diagnostics(text_document(font="unregistered-font.shx")) == []
    assert lookups and all(row == ("registered-alias.shx", "registered-alias.shx") for row in lookups)


def test_bigfont_and_general_substitution_warnings_coexist_with_png(tmp_path):
    doc = text_document(bigfont="synthetic-missing-big.shx")
    before = doc.styles.get("Standard").dxf.all_existing_dxf_attribs().copy()
    target = tmp_path / "preview.png"
    reasons = render.render_frame(doc, Frame(doc.modelspace(), "frame", (0, 0, 100, 80), []), target)
    assert target.read_bytes().startswith(b"\x89PNG")
    assert any(row.startswith("BIGFONT 字体格式") for row in reasons)
    assert any("缺字体" in row and "字形与字宽和原图不同" in row for row in reasons)
    assert doc.styles.get("Standard").dxf.all_existing_dxf_attribs() == before


def test_worker_keeps_explicit_bigfont_incomplete_when_every_preview_exists(stub_tools, fixture_dxf, request_dwg):
    doc = ezdxf.readfile(fixture_dxf)
    doc.styles.get("Standard").dxf.bigfont = "synthetic-declared-big.shx"
    doc.saveas(fixture_dxf)
    result = parse_dwg_file(request_dwg, options=DwgOptions(timeout_sec=10, worker_timeout_sec=120))
    assert len(result.result.units) == 2
    assert all(unit.preview_relative_path for unit in result.result.units)
    assert result.status == DwgFileStatus.PARTIAL
    assert any(row.startswith("BIGFONT 字体格式") for row in result.result.incomplete)
    assert any("缺字体" in row for row in result.result.incomplete)
    assert result.statistics["font_diagnostics"][0]["code"] == "FONT_FORMAT_UNSUPPORTED_BIGFONT"
    assert result.sha256_before == result.sha256_after
