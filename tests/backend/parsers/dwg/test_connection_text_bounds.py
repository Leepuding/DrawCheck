"""The connection evidence must point to the glyphs actually drawn in the viewer."""
import ezdxf
from ezdxf import bbox

from backend.parsers.dwg.engineering import native_connections
from backend.parsers.dwg.render import rendered_text_bounds


def test_connection_inventory_shares_rendered_boxes_for_rotated_and_cjk_text():
    doc = ezdxf.new('R2018')
    layout = doc.modelspace()
    entities = [layout.add_text(text, dxfattribs={'height': 2.5, 'insert': (x, 20), 'rotation': rotation})
                for text, rotation, x in (('CAB01', 0, 10), ('D11', 90, 30), ('I1111', 0, 50), ('开关柜编号', 0, 70))]
    rendered, _ = rendered_text_bounds(doc, entities)
    records = {record['handle']: record for record in native_connections(layout, bbox.Cache())}
    for entity, expected in zip(entities, rendered):
        assert expected is not None
        actual = records[entity.dxf.handle]['bounds']
        assert all(abs(a-b) < 1e-9 for a, b in zip(actual, expected))


def test_failed_glyph_bounds_do_not_fall_back_to_an_estimated_text_box(monkeypatch):
    doc = ezdxf.new('R2018')
    layout = doc.modelspace()
    text = layout.add_text('CAB01', dxfattribs={'height': 2.5})
    line = layout.add_line((0, 0), (0, 20))
    monkeypatch.setattr('backend.parsers.dwg.render.rendered_text_bounds',
                        lambda doc, entities: ([None for _ in entities], [{'code': 'TEXT_RENDER_FAILED'}]))
    records = list(native_connections(layout, bbox.Cache()))
    assert not any(record['handle'] == text.dxf.handle for record in records)
    kept = next(record for record in records if record['handle'] == line.dxf.handle)
    assert kept['points'] == [(0, 0), (0, 20)] and kept['bounds'] == (0, 0, 0, 20)
