from types import SimpleNamespace

import pytest

from backend.importer.project_codes import project_code_notice


@pytest.mark.parametrize("b, expected", [
    ("33-N16091S-T0107-01",True), ("33-NZ16181S-T0103-02",False), (None,False),
    ("T0103-02",False),
])
def test_batch_notice_needs_two_verified_project_codes(monkeypatch,b,expected):
    from backend.importer import project_codes
    titles = {"a":"33-NZ16181S-T0102-01", "b":b}
    def read_title(row, **_):
        number = titles[row["id"]]
        return {"number":number,"value":{"text":number,"bbox":{"x0":.6,"y0":.8,"x1":.8,"y1":.85}}} if number else None
    monkeypatch.setattr(project_codes,"LocalSheetReader",lambda _: SimpleNamespace(read_import_title=read_title))
    units = [{"id":uid,"kind":"pdf_page","ordinal":1} for uid in titles]
    repo = SimpleNamespace(list_records=lambda *_,**__:units)
    drawings = [{"unit_id":uid,"source_original":f"33-N16091S-{uid}.pdf"} for uid in titles]
    result = project_code_notice(repo,"v",drawings)
    assert bool(result) is expected
    if expected:
        assert len(result["evidence"]) == 2 and result["code"] == "MIXED_PROJECT_CODES"


def test_large_loose_sheet_batch_uses_native_probes_without_unbounded_ocr(monkeypatch):
    from backend.importer import project_codes
    probes = []
    def read_title(unit, *, scan):
        probes.append((unit["id"], scan))
        return None
    monkeypatch.setattr(project_codes, "LocalSheetReader", lambda _: SimpleNamespace(read_import_title=read_title))
    units = [{"id": f"u{i}", "kind": "pdf_page", "ordinal": 1} for i in range(12)]
    drawings = [{"unit_id": row["id"], "original_id": row["id"], "source_original": "单张.pdf"} for row in units]
    assert project_code_notice(SimpleNamespace(list_records=lambda *_, **__: units),"v",drawings) is None
    assert len(probes) == len(units) and not any(scan for _, scan in probes)


def test_native_text_probe_distinguishes_vector_geometry_and_searchable_text(tmp_path):
    from reportlab.pdfgen import canvas
    from backend.extract.local_sheet import _may_have_native_text

    for has_text in (False, True):
        path = tmp_path / f"sheet-{has_text}.pdf"
        doc = canvas.Canvas(str(path))
        doc.line(0, 20, 200, 20)
        if has_text:
            doc.drawString(40, 40, "33-NZ16181S-T0102-01")
        doc.save()
        assert _may_have_native_text(path) is has_text
