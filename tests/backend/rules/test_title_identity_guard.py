"""Uncertain model titles cannot establish missing drawings; human corrections win."""
import json

import pytest

from backend.catalog import CatalogService
from backend.extract import ExtractService, ExtractResult, PROMPT_VERSION
from backend.rules.catalog_titleblock import run_catalog_titleblock
from tests.backend.rules.test_rules_b import seed


def setup(repo,tmp_path):
    version_id,original_id=seed(repo,tmp_path)
    repo.create_unit(version_id,original_id,kind="pdf_page",ordinal=1,name="目录",drawing_number="CAT",
        metadata={"extract_status":"ok","is_catalog_sheet":True,"catalog_listed_numbers":["S-001"],"catalog_complete":True})
    unit=repo.create_unit(version_id,original_id,kind="pdf_page",ordinal=2,name="文件第2页",drawing_number="2",metadata={"extract_status":"ok"})
    return version_id, unit


def result(vid,unit,**changes):
    block={"drawing_number":"S-001","name":"基础图","bbox":{"x0":.7,"y0":.8,"x1":.9,"y1":.9},"confidence":.9,"unclear":False}
    block.update(changes)
    return ExtractResult(unit["id"],vid,"ok",PROMPT_VERSION,title_blocks=[block])


@pytest.mark.parametrize("changes", [{"unclear":True},{"confidence":.2},{"no_title_block":True},{"drawing_number":""}])
def test_uncertain_title_preserves_placeholder_and_blocks_missing_conclusions(repo,tmp_path,changes):
    vid,unit=setup(repo,tmp_path)
    data=result(vid,unit,**changes)
    # Production extraction metadata must carry the same uncertainty as catalog synchronization.
    ExtractService(repo,use_mock=False)._persist(data,unit)
    assert repo.get("drawing_units",unit["id"])["metadata"]["title_identity_uncertain"]
    CatalogService(repo).apply_extract_result(data)
    stored=repo.get("drawing_units",unit["id"])
    assert stored["drawing_number"] == "2"
    assert stored["metadata"]["title_identity_uncertain"]
    review=run_catalog_titleblock(repo,vid)
    assert review["incomplete"]
    assert review["issues"] == []


def test_empty_title_is_unverified_but_reliable_title_updates_real_directory(repo,tmp_path):
    vid,unit=setup(repo,tmp_path)
    catalog=CatalogService(repo)
    catalog.apply_extract_result(ExtractResult(unit["id"],vid,"ok",PROMPT_VERSION))
    assert repo.get("drawing_units",unit["id"])["metadata"]["title_identity_uncertain"]
    catalog.apply_extract_result(result(vid,unit))
    current=repo.get("drawing_units",unit["id"])
    assert current["drawing_number"] == "S-001" and current["name"] == "基础图"
    assert not current["metadata"]["title_identity_uncertain"]
    assert run_catalog_titleblock(repo,vid) == {"issues":[],"incomplete":[]}


def test_manual_drawing_number_remains_authoritative_after_unclear_or_missing_title(repo,tmp_path):
    vid,unit=setup(repo,tmp_path)
    catalog=CatalogService(repo)
    catalog.apply_extract_result(result(vid,unit,confidence=.2))
    catalog.rename(unit_id=unit["id"],drawing_number="S-001",name="人工基础图")
    corrected=repo.get("drawing_units",unit["id"])
    assert not corrected["metadata"]["title_identity_uncertain"]
    for data in (result(vid,unit,drawing_number="WRONG-099",unclear=True), ExtractResult(unit["id"],vid,"ok",PROMPT_VERSION)):
        latest=repo.get("drawing_units",unit["id"])
        ExtractService(repo,use_mock=False)._persist(data,latest)
        catalog.apply_extract_result(data)
        current=repo.get("drawing_units",unit["id"])
        assert current["drawing_number"] == "S-001" and current["name"] == "人工基础图"
        assert not current["metadata"]["title_identity_uncertain"] and not current["metadata"]["no_title_block"]
        assert current["metadata"]["title_drawing_number"] == "S-001"
        assert run_catalog_titleblock(repo,vid) == {"issues":[],"incomplete":[]}
