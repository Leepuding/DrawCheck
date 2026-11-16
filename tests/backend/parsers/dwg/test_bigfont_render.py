"""Actual BIGFONT stroke/advance support, with strict negative cases."""
import struct
from pathlib import Path

import ezdxf
import pytest

from backend.parsers.dwg.fonts import Bigfont, FontUnsupported, font_for_style
from backend.parsers.dwg import render
from backend.parsers.dwg.frames import Frame


def compiled_bigfont(entries=None):
    if entries is None:
        # #1 saves the parent insertion point; #2 restores it then advances.
        entries = {0: b'Synthetic\0\x7f\0\x02\0',
                   1: bytes([0, 5, 2, 0]),
                   2: bytes([0, 6, 2, 8, 127, 0, 0]),
                   0xD6D0: bytes([0, 7, 1, 8, 10, 10, 1, 8, 20, 50, 7, 2, 0])}
    prefix = b'AutoCAD-86 bigfont 1.0\r\n\x1a' + struct.pack('<HHHHH', 8, len(entries), 1, 0xA1, 0xFE)
    offset = len(prefix) + len(entries)*8
    index, chunks = bytearray(), bytearray()
    for code, data in entries.items():
        index.extend(struct.pack('<HHI', code, len(data), offset+len(chunks)))
        chunks.extend(data)
    return prefix + index + chunks


def test_gbk_code_and_shared_subshape_stack_produce_strokes_and_advance():
    font = Bigfont(compiled_bigfont())
    assert font.code_for('中') == 0xD6D0
    glyph = font.glyph('中')
    assert glyph.end.x == 127
    assert glyph.end.y == 0
    vertices = list(glyph.control_vertices())
    assert any(tuple(v) == (30, 60) for v in vertices)
    glyph.transform_inplace(__import__('ezdxf').math.Matrix44.scale(2, 2, 1))
    assert font.glyph('中').end.x == 127  # immutable cache, repeated input


@pytest.mark.parametrize('char', ['文', '浬', '😀'])
def test_missing_glyph_and_outside_declared_encoding_fail(char):
    with pytest.raises(FontUnsupported):
        Bigfont(compiled_bigfont()).glyph(char)


@pytest.mark.parametrize('data', [b'', b'AutoCAD-86 bigfont 1.0\r\n\x1a', compiled_bigfont()[:-3]])
def test_bad_files_rejected(data):
    with pytest.raises(FontUnsupported):
        Bigfont(data)


@pytest.mark.parametrize('body', [bytes([0, 15, 0]), bytes([0, 6, 0]),
                                 bytes([0, 7, 0, 0]), bytes([0, 7, 3, 0]),
                                 bytes([0, 5, 0]), bytes([0, 3, 0, 0]),
                                 bytes([0, 2, 8, 127, 0, 0])])
def test_unknown_bad_and_extended_bytecode_never_claims_support(body):
    with pytest.raises(FontUnsupported):
        Bigfont(compiled_bigfont({0:b'F\0\x7f\0\x02\0', 0xD6D0:body})).glyph('中')


def test_duplicate_index_character_rejected():
    data = bytearray(compiled_bigfont())
    start = len(b'AutoCAD-86 bigfont 1.0\r\n\x1a') + 10
    struct.pack_into('<H', data, start+8, 0)
    with pytest.raises(FontUnsupported, match='duplicate'):
        Bigfont(data)


@pytest.fixture
def installed_style():
    root = Path('/Applications/gstarcad.app/Contents/Resources/Fonts')
    if not (root/'tssdeng.shx').is_file():
        pytest.skip('local licensed CAD fonts not installed; no fonts bundled')
    doc = ezdxf.new()
    style = doc.styles.get('Standard')
    style.dxf.font = str(root/'tssdeng.shx')
    style.dxf.bigfont = str(root/'tssdchn.shx')
    return doc, style


def test_actual_local_fonts_have_primary_and_gbk_advances(installed_style):
    _, style = installed_style
    font = font_for_style(style)
    assert font.text_width_ex('348', 400, .7) == pytest.approx(646.1538461538461)
    assert font.text_width_ex('中文图纸', 4, .7) == pytest.approx(11.2)
    assert len(font.text_glyph_paths('348中文±°Φ', 4)) == 8
    with pytest.raises(FontUnsupported):
        font.text_width('😀')


def test_missing_bigfont_preserves_supported_primary_glyphs(installed_style, tmp_path):
    doc, style = installed_style
    style.dxf.bigfont = "unavailable-paired-font.shx"
    model = doc.modelspace()
    latin = model.add_text("348", dxfattribs={"height": 4, "insert": (1, 8)})
    before, _ = render.rendered_text_bounds(doc, [latin])
    assert before[0] is not None
    assert render.bigfont_diagnostics(doc)[0]["code"] == "FONT_BIGFONT_NOT_USED"
    frame = Frame(model, "test", (0, 0, 40, 15), [])
    assert not any("unavailable-paired-font.shx" in reason for reason in render.render_frame(doc, frame, tmp_path / "primary.png"))

    model.add_text("中文", dxfattribs={"height": 4, "insert": (1, 2)})
    after, _ = render.rendered_text_bounds(doc, [latin])
    assert before == after
    row = render.bigfont_diagnostics(doc)[0]
    assert row["code"] == "FONT_BIGFONT_MISSING"
    assert set(row["failed_characters"]) == {"中", "文"}
    assert any("unavailable-paired-font.shx" in reason for reason in render.render_frame(doc, frame, tmp_path / "mixed.png"))


def test_png_and_pdf_use_same_local_strokes_and_keep_source_unchanged(installed_style, tmp_path):
    doc, style = installed_style
    doc.modelspace().add_text('348中文±°Φ', dxfattribs={'height':4, 'insert':(1,5)})
    frame = Frame(doc.modelspace(), 'test', (0,0,40,15), [])
    before = style.dxf.all_existing_dxf_attribs().copy()
    diagnostics = render.bigfont_diagnostics(doc)
    assert diagnostics[0]['resolved'] is True
    assert diagnostics[0]['native_cad_compared'] is False
    assert len(diagnostics[0]['local_files']) == 2
    for suffix, signature in [('.png',b'\x89PNG'),('.pdf',b'%PDF')]:
        target = tmp_path/f'preview{suffix}'
        reasons = render.render_frame(doc, frame, target)
        assert target.read_bytes().startswith(signature)
        assert not any('fallback font' in row or '缺字体' in row for row in reasons)
    assert style.dxf.all_existing_dxf_attribs() == before


def test_one_missing_character_has_explicit_fallback_without_replacing_supported_style(installed_style, tmp_path):
    doc, style = installed_style
    doc.modelspace().add_text('中文', dxfattribs={'height':4, 'insert':(1,8)})
    doc.modelspace().add_text('😀', dxfattribs={'height':4, 'insert':(1,2)})
    row = render.bigfont_diagnostics(doc)[0]
    assert row['resolved'] is False
    assert set(row['failed_characters']) == {'😀'}
    reasons = render.render_frame(doc, Frame(doc.modelspace(),'test',(0,0,40,15),[]),tmp_path/'partial.png')
    assert any('explicit fallback font' in reason for reason in reasons)
    # A supportable entity still resolves to the exact SHX cache, independently
    # of the failed entity and of previous document text engines.
    from ezdxf.addons.drawing import RenderContext
    from ezdxf.addons.drawing.recorder import Recorder
    ctx1, ctx2 = RenderContext(doc), RenderContext(doc)
    ctx1._drawcheck_fonts = {'local.shx': font_for_style(style)}
    one = render._frontend(ctx1, Recorder())
    two = render._frontend(ctx2, Recorder())
    assert one.text_engine is not two.text_engine
    assert 'local.shx' in one.text_engine._font_cache
    assert 'local.shx' not in two.text_engine._font_cache


def test_mtext_cjk_wraps_inside_original_column_and_latin_model_stays_intact(installed_style):
    doc, style = installed_style
    style.dxf.width = .67
    entity = doc.modelspace().add_mtext('高压加热照明电源\\P16A', dxfattribs={
        'insert':(10,50), 'char_height':3.5, 'width':3.178235465329681})
    before = entity.text
    context, _ = render._render_context(doc)
    from ezdxf.addons.drawing.unified_text_renderer import UnifiedTextRenderer
    class Capture:
        def __init__(self):
            self.calls = []
            self.text_engine = UnifiedTextRenderer()
            self.text_engine._font_cache.update(context._drawcheck_fonts)
        def draw_text(self, text, matrix, properties, cap_height):
            self.calls.append((text, tuple(matrix.transform((0,0,0))), matrix))
    pipeline = Capture()
    properties = context.resolve_all(entity)
    render._cjk_mtext_renderer(context,pipeline,entity,properties)
    chinese = [row for row in pipeline.calls if row[0] != '16A']
    assert ''.join(row[0] for row in chinese) == '高压加热照明电源'
    assert len(chinese) == 8
    assert len({round(row[1][0],6) for row in chinese}) == 1
    assert len({round(row[1][1],6) for row in chinese}) == 8
    assert [row[0] for row in pipeline.calls].count('16A') == 1
    assert entity.text == before


def test_actual_bounds_keep_rotation_alignment_wrap_and_no_fake_empty_boxes(installed_style):
    doc, style = installed_style
    style.dxf.width = .67
    mtext = doc.modelspace().add_mtext('变压器',dxfattribs={
        'insert':(10,50), 'char_height':3.36, 'width':5.060928})
    rotated = doc.modelspace().add_text('348',dxfattribs={
        'insert':(30,30),'height':4,'rotation':90,'width':.7})
    from ezdxf.enums import TextEntityAlignment
    centered = doc.modelspace().add_text('348',dxfattribs={'height':4,'width':.7})
    centered.set_placement((60,30),align=TextEntityAlignment.MIDDLE_CENTER)
    empty = doc.modelspace().add_text(' ',dxfattribs={'height':4})
    boxes, diagnostics = render.rendered_text_bounds(doc,[mtext,rotated,centered,empty])
    assert boxes[0][2]-boxes[0][0] <= 5.060928 + 1e-6
    assert boxes[0][3]-boxes[0][1] > 3.36*1.5
    assert boxes[1][3]-boxes[1][1] > boxes[1][2]-boxes[1][0]
    assert boxes[2][0] < 60 < boxes[2][2]
    assert boxes[2][1] < 30 < boxes[2][3]
    assert boxes[3] is None
    assert any(row['code']=='TEXT_NO_RENDERED_BOUNDS' and row['index']==3 for row in diagnostics)
