"""Actual DXF -> frontend primitives -> PNG, compared with the former full-layout render."""
from dataclasses import replace

import ezdxf
from PIL import Image, ImageChops
import pytest

from backend.parsers.dwg.frames import Frame
from backend.parsers.dwg import render


@pytest.fixture
def many_frames():
    doc = ezdxf.new("R2010")
    model = doc.modelspace()
    block = doc.blocks.new("nested-shapes")
    block.add_circle((0, 0), 8)
    block.add_text("nested", dxfattribs={"insert":(-5, -2), "height":2})
    frames = []
    for n in range(6):
        x = n*180
        bounds = (x, 0, x+100, 80)
        frames.append(Frame(model, str(n), bounds, []))
        model.add_lwpolyline([(x,0),(x+100,0),(x+100,80),(x,80)], close=True)
        for row in range(30):
            model.add_line((x+5, row*2+5), (x+65, row*2+5))
        model.add_text(f"Sheet {n}", dxfattribs={"insert":(x+10,70), "height":4, "rotation":12})
        model.add_blockref("nested-shapes", (x+65,40), dxfattribs={"rotation":30, "xscale":1.5})
        model.add_arc((x+50,50), 25, 20, 250)
        model.add_spline([(x+2,10),(x+50,90),(x+105,20)])
    # Endpoints and insertion point are outside the first frame, but these cross it.
    crossing = model.add_line((-100,40),(1100,40), dxfattribs={"color":1,"lineweight":211})
    outside_insert = model.add_blockref("nested-shapes", (-3,30), dxfattribs={"xscale":2})
    return doc, frames, crossing, outside_insert


def assert_same_png(a, b):
    with Image.open(a) as first, Image.open(b) as second:
        assert first.size == second.size
        assert ImageChops.difference(first.convert("RGB"), second.convert("RGB")).getbbox() is None


def test_shared_recording_reduces_candidates_and_preserves_cross_frame_png(many_frames, tmp_path, monkeypatch):
    from ezdxf.addons.drawing import Frontend
    doc, frames, crossing, outside_insert = many_frames
    calls = []
    original_draw = Frontend.draw_layout
    def counted(self, *args, **kwargs):
        calls.append(1)
        return original_draw(self, *args, **kwargs)
    monkeypatch.setattr(Frontend, "draw_layout", counted)
    optimized = []
    candidates = []
    source_bounds = None
    for index, frame in enumerate(frames):
        path = tmp_path / f"optimized-{index}.png"
        render.render_frame(doc, frame, path)
        optimized.append(path)
        recording = next(iter(render._RECORDINGS[doc].values()))
        candidates.append(recording.selected_count)
        assert recording.selected_count < len(recording.player.records)/3
        if source_bounds is None:
            source_bounds = list(recording.bounds)
        else:
            assert recording.bounds == source_bounds  # No per-frame in-place clipping of the shared recording.
    assert len(calls) == 1
    recording = next(iter(render._RECORDINGS[doc].values()))
    selected = render._select_records(recording, frames[0].bounds)
    handles = {record.handle for record in selected.records}
    assert crossing.dxf.handle in handles
    assert outside_insert.dxf.handle in handles
    # Re-run the prior code path as the visual reference, with identical fonts/DPI/axis bounds.
    def no_recorder(*args):
        raise RuntimeError("force reference full-layout rendering")
    monkeypatch.setattr(render, "_layout_recording", no_recorder)
    for index in (0, 3):
        reference = tmp_path / f"reference-{index}.png"
        reasons = render.render_frame(doc, frames[index], reference)
        assert any("完整布局路径" in reason for reason in reasons)
        assert_same_png(reference, optimized[index])
    assert len(calls) == 3
    print({"records":len(recording.player.records), "selected_per_frame":candidates, "layout_draws_for_six_frames":1})


def test_unknown_bounds_retained_and_not_silently_dropped(many_frames, tmp_path, monkeypatch):
    doc, frames, *_ = many_frames
    path = tmp_path / "known.png"
    render.render_frame(doc, frames[0], path)
    recording = next(iter(render._RECORDINGS[doc].values()))
    class UnknownBounds:
        def bbox(self):
            raise ValueError("unknown")
    assert render._record_bounds(UnknownBounds()) is None
    all_unknown = replace(recording, bounds=[None]*len(recording.player.records))
    selected = render._select_records(all_unknown, frames[0].bounds)
    assert selected.records == recording.player.records
    assert selected.records is not recording.player.records


def test_unsupported_record_uses_full_layout_path(many_frames, tmp_path, monkeypatch):
    doc, frames, *_ = many_frames
    optimized = tmp_path / "optimized.png"
    render.render_frame(doc, frames[0], optimized)
    recording = next(iter(render._RECORDINGS[doc].values()))
    unsupported = replace(recording, supported=False)
    monkeypatch.setattr(render, "_layout_recording", lambda *args: unsupported)
    fallback = tmp_path / "fallback.png"
    reasons = render.render_frame(doc, frames[0], fallback)
    assert any("无法可靠回放" in reason for reason in reasons)
    assert_same_png(optimized, fallback)
