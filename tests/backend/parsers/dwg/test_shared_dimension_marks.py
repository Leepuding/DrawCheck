import ezdxf
import pytest
from ezdxf.xclip import XClip

from backend.parsers.dwg.dimension_marks import read_shared_dimensions
from backend.rules.dwg_dimensions import run_dwg_dimensions
from backend.store import BBox, Evidence, Repository, Source


def site_drawing(*, offset=0, display="40 mm", changed_line=False):
    doc = ezdxf.new("R2010")
    block = doc.blocks.new("site-base")
    for y in (2, 10, 20, 40):
        block.add_line((0, y), (70 + int(changed_line and y == 2), y))
    for x in (2, 10, 35, 50):
        block.add_line((x, 0), (x, 60))
    dim = block.add_linear_dim(base=(0, 32), p1=(5, 5), p2=(45, 5), angle=0, text=display, dimstyle="Standard")
    dim.render()
    insert = doc.modelspace().add_blockref("site-base", (offset, 5), dxfattribs={"extrusion": (1e-16, 0, 1)})
    return doc, insert, (offset-4, 0, offset+90, 90)


def marks(doc, bounds):
    return read_shared_dimensions(doc.modelspace(), [bounds])[bounds]


def test_same_site_lines_and_endpoints_survive_translation_and_different_display_values():
    left, _, bounds_left = site_drawing()
    right, _, bounds_right = site_drawing(offset=1000, display="42 mm")
    different, _, bounds_diff = site_drawing(display="40 mm", changed_line=True)
    a, = marks(left, bounds_left)
    b, = marks(right, bounds_right)
    c, = marks(different, bounds_diff)
    assert a["shared_block_mark"]["key"] == b["shared_block_mark"]["key"]
    assert a["shared_block_mark"]["line_basis_sha256"] == b["shared_block_mark"]["line_basis_sha256"]
    assert a["shared_block_mark"]["line_basis_sha256"] != c["shared_block_mark"]["line_basis_sha256"]
    assert (a["evidence"]["text"], b["evidence"]["text"]) == ("40 mm", "42 mm")
    assert a["value"] == "40" and b["value"] == "42"
    assert a["evidence"]["bbox"] == pytest.approx(b["evidence"]["bbox"], abs=.01)


def test_repeated_base_and_clipped_dimension_are_not_eligible():
    doc, insert, bounds = site_drawing()
    clip = XClip(insert)
    clip.set_block_clipping_path([(-3, -1), (85, 78)])
    assert len(marks(doc, bounds)) == 1
    clip.set_block_clipping_path([(-3, -1), (30, 78)])
    assert marks(doc, bounds) == []
    clip.disable_clipping()
    assert len(marks(doc, bounds)) == 1
    doc.modelspace().add_blockref("site-base", (200, 5))
    assert marks(doc, bounds) == []


def _store_units(tmp_path, store, version_id, rows, *, same_file=False):
    units = []
    for ordinal, measurements in enumerate(rows, 1):
        path = tmp_path / f"source{ordinal}.dwg"
        path.write_bytes(b"AC1024" + (b"same" if same_file else bytes([ordinal])))
        source = store.import_original(version_id, path)["record"]
        units.append(store.create_unit(version_id, source["id"], kind="dwg_frame", ordinal=ordinal,
                                       name=f"卷册{ordinal}", drawing_number=f"D000{ordinal}-02",
                                       metadata={"preview_relative_path": "unit.png", "native_measurements": measurements}))
    return units


@pytest.mark.parametrize(("other", "expected"), [
    ("4 cm", "match"), ("41 mm", "match"), ("42 mm", "mismatch"), ("42", "unverified"),
])
def test_rule_compares_only_located_marks_with_units(tmp_path, other, expected):
    first, _, bounds = site_drawing()
    second, _, other_bounds = site_drawing(offset=500, display=other)
    with Repository(tmp_path / "project") as store:
        project = store.create_project("尺寸复核")
        version = store.create_version(project["id"], "图纸")["id"]
        units = _store_units(tmp_path, store, version, [marks(first, bounds), marks(second, other_bounds)])
        result = run_dwg_dimensions(store, version, units=units)
        pair, = result["comparisons"]
        assert result["positions"] == 1 and pair["outcome"] == expected
        assert {pair["left_evidence"]["text"], pair["right_evidence"]["text"]} == {"40 mm", other}
        issues = store.list_records("issues", version_id=version)
        assert len(issues) == (expected == "mismatch")
        if issues:
            assert issues[0]["rule_result"]["max_abs_diff"] == "2"
            assert issues[0]["left_unit_id"] != issues[0]["right_unit_id"]
            run_dwg_dimensions(store, version, units=units)
            assert len(store.list_records("issues", version_id=version)) == 1
        assert not store.list_records("pending_confirmations", version_id=version)


def test_duplicate_file_and_manual_uncertainty_do_not_create_issues(tmp_path):
    first, _, bounds = site_drawing()
    second, _, other_bounds = site_drawing(offset=200, display="42 mm")
    a, b = marks(first, bounds), marks(second, other_bounds)
    with Repository(tmp_path / "project") as store:
        project = store.create_project("重复与人工")
        duplicate = store.create_version(project["id"], "重复")["id"]
        copies = _store_units(tmp_path, store, duplicate, [a, a], same_file=True)
        assert run_dwg_dimensions(store, duplicate, units=copies)["comparisons"] == []
        version = store.create_version(project["id"], "人工")["id"]
        units = _store_units(tmp_path, store, version, [a, b])
        pending = store.create_pending(version, Evidence(units[0]["id"], Source.CAD_TEXT, "40 mm",
                                                        BBox(**a[0]["evidence"]["bbox"])), "核对原标注")
        store.confirm_pending(pending["id"], status="无法确定")
        result = run_dwg_dimensions(store, version, units=units)
        assert result["comparisons"][0]["outcome"] == "unverified"
        assert result["issues"] == []


def test_three_identical_unitless_readings_yield_one_unverified_mark(tmp_path):
    doc, _, bounds = site_drawing(display="40")
    rows = marks(doc, bounds)
    with Repository(tmp_path / "project") as store:
        project = store.create_project("同一底图")
        version = store.create_version(project["id"], "图纸")["id"]
        units = _store_units(tmp_path, store, version, [rows, rows, rows])
        pair, = run_dwg_dimensions(store, version, units=units)["comparisons"]
        assert pair["outcome"] == "unverified" and pair["source_file_count"] == 3
        assert "两张图均标注 40" in pair["basis"]
