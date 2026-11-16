"""DC-11 关联：依据类型与禁止仅同名/同数字；W7-Q2=B 同名 pending。"""
from __future__ import annotations

from backend.linking import (
    LINK_KIND_CATALOG_REF,
    LINK_KIND_DETAIL_INDEX,
    LINK_KIND_SHARED_NUMBER,
    LinkingService,
)
from backend.linking.service import SAME_NAME_PENDING_REASON
from backend.store import BBox, Evidence, Source


def make_version(repo, tmp_path, *, name: str = "关联测试"):
    source = tmp_path / "link.pdf"
    source.write_bytes(b"%PDF-LINK\n")
    project = repo.create_project(name)
    version = repo.create_version(project["id"], "第一版")
    original = repo.import_original(version["id"], source)["record"]
    return project, version, original


def _fact(repo, version_id, unit_id, text, bbox, *, unit=None, numeric=None):
    return repo.add_fact(
        version_id,
        Evidence(unit_id, Source.VISION, text, BBox(*bbox)),
        numeric_value=numeric,
        unit=unit,
    )


def test_link_by_titleblock_and_catalog_ref(repo, tmp_path):
    _, version, original = make_version(repo, tmp_path)
    vid = version["id"]
    repo.create_unit(
        vid, original["id"], kind="pdf_page", ordinal=1, name="图纸目录",
        drawing_number="S-000",
        metadata={"is_catalog_sheet": True, "catalog_listed_numbers": ["S-001", "S-002"]},
    )
    repo.create_unit(
        vid, original["id"], kind="pdf_page", ordinal=2, name="平面", drawing_number="S-001", metadata={},
    )
    repo.create_unit(
        vid, original["id"], kind="pdf_page", ordinal=3, name="详图", drawing_number="S-002", metadata={},
    )
    result = LinkingService(repo).link_version(vid)
    kinds = {a["kind"] for a in result.associations}
    assert LINK_KIND_CATALOG_REF in kinds
    for a in result.associations:
        details = a["details"]
        assert details.get("basis")
        assert "confidence" in details
        assert details["confidence"] > 0


def test_link_by_detail_index_and_shared_number(repo, tmp_path):
    _, version, original = make_version(repo, tmp_path)
    vid = version["id"]
    u1 = repo.create_unit(vid, original["id"], kind="pdf_page", ordinal=1, name="索引", drawing_number="S-006", metadata={})
    u2 = repo.create_unit(vid, original["id"], kind="pdf_page", ordinal=2, name="详图", drawing_number="S-002", metadata={})
    u3 = repo.create_unit(vid, original["id"], kind="pdf_page", ordinal=3, name="材料", drawing_number="S-004", metadata={})
    _fact(repo, vid, u1["id"], "见详图 S-002", (0.1, 0.1, 0.2, 0.15))
    _fact(repo, vid, u2["id"], "XB-03", (0.2, 0.2, 0.3, 0.25))
    _fact(repo, vid, u3["id"], "XB-03", (0.4, 0.4, 0.5, 0.45))
    result = LinkingService(repo).link_version(vid)
    kinds = {a["kind"] for a in result.associations}
    assert LINK_KIND_DETAIL_INDEX in kinds
    assert LINK_KIND_SHARED_NUMBER in kinds


def test_reject_name_only_and_digit_only_association(repo, tmp_path):
    _, version, original = make_version(repo, tmp_path)
    vid = version["id"]
    u1 = repo.create_unit(vid, original["id"], kind="pdf_page", ordinal=1, name="A", drawing_number="S-010", metadata={})
    u2 = repo.create_unit(vid, original["id"], kind="pdf_page", ordinal=2, name="B", drawing_number="S-011", metadata={})
    _fact(repo, vid, u1["id"], "基础F1", (0.1, 0.1, 0.2, 0.15))
    _fact(repo, vid, u2["id"], "基础F1", (0.1, 0.1, 0.2, 0.15))
    _fact(repo, vid, u1["id"], "100", (0.3, 0.3, 0.35, 0.34), unit=None)
    _fact(repo, vid, u2["id"], "100", (0.3, 0.3, 0.35, 0.34), unit=None)
    result = LinkingService(repo).link_version(vid)
    cross = [
        a for a in result.associations
        if {a["left_unit_id"], a["right_unit_id"]} == {u1["id"], u2["id"]}
    ]
    assert cross == []
    assert result.rejected_name_only >= 1


def test_same_name_ambiguity_pending_w7q2_b(repo, tmp_path):
    """ISSUE-SAME-NAME：0 疑似 + 1 待确认（W7-Q2=B）。"""
    _, version, original = make_version(repo, tmp_path)
    vid = version["id"]
    u = repo.create_unit(
        vid, original["id"], kind="pdf_page", ordinal=1, name="同名页", drawing_number="S-010", metadata={},
    )
    _fact(repo, vid, u["id"], "基础F1", (0.064, 0.78, 0.105, 0.802))
    _fact(repo, vid, u["id"], "基础F1", (0.232, 0.84, 0.273, 0.861))
    _fact(repo, vid, u["id"], "支架Z1", (0.064, 0.86, 0.105, 0.885))
    result = LinkingService(repo).link_version(vid)
    issues = repo.list_records("issues", version_id=vid)
    pendings = repo.list_records("pending_confirmations", version_id=vid)
    assert issues == []
    assert len(result.same_name_pending) == 1
    assert len(pendings) == 1
    assert pendings[0]["reason"] == SAME_NAME_PENDING_REASON
    assert pendings[0]["text"] == "基础F1"
    assert sum(1 for p in pendings if p["text"] == "支架Z1") == 0


def test_same_name_ok_zero_pending(repo, tmp_path):
    _, version, original = make_version(repo, tmp_path)
    vid = version["id"]
    u = repo.create_unit(
        vid, original["id"], kind="pdf_page", ordinal=1, name="同名页", drawing_number="S-010", metadata={},
    )
    _fact(repo, vid, u["id"], "支架Z1", (0.064, 0.86, 0.105, 0.885))
    LinkingService(repo).link_version(vid)
    assert repo.list_records("issues", version_id=vid) == []
    assert repo.list_records("pending_confirmations", version_id=vid) == []
