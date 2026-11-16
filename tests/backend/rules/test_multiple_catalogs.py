from backend.rules.catalog_titleblock import run_catalog_titleblock
from tests.backend.rules.test_rules_b import seed


def unit(repo, vid, oid, no, **metadata):
    return repo.create_unit(vid,oid,kind="pdf_page",ordinal=len(repo.list_records("drawing_units",version_id=vid))+1,
        name=no,drawing_number=no,metadata={"extract_status":"ok", **metadata})


def test_all_catalogs_merged_and_catalog_volume_ids_not_missing(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    unit(repo,vid,oid,"CAT-A",is_catalog_sheet=True,catalog_complete=True,catalog_listed_numbers=["CAT-A","VOL-A","A-001"],catalog_volume_numbers=["VOL-A"])
    unit(repo,vid,oid,"CAT-B",is_catalog_sheet=True,catalog_complete=True,catalog_listed_numbers=["CAT-B","B-001"])
    unit(repo,vid,oid,"A-001")
    unit(repo,vid,oid,"B-001")
    result=run_catalog_titleblock(repo,vid)
    assert result["issues"] == [] and result["incomplete"] == []


def test_missing_sheet_uses_catalog_that_actually_lists_it(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    a=unit(repo,vid,oid,"CAT-A",is_catalog_sheet=True,catalog_complete=True,catalog_listed_numbers=["A-001"])
    b=unit(repo,vid,oid,"CAT-B",is_catalog_sheet=True,catalog_complete=True,catalog_listed_numbers=["B-099"])
    unit(repo,vid,oid,"A-001")
    result=run_catalog_titleblock(repo,vid)
    issue=next(i for i in result["issues"] if i["rule_result"].get("fixture_id")=="ISSUE-CAT-MISSING")
    assert issue["left_unit_id"] == b["id"] and issue["left_unit_id"] != a["id"]
    assert issue["left_text"] == "B-099"


def test_incomplete_other_catalog_prevents_unlisted_and_count_false_positive(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    unit(repo,vid,oid,"CAT-A",is_catalog_sheet=True,catalog_complete=True,catalog_listed_numbers=["A-001"])
    unit(repo,vid,oid,"CAT-B",is_catalog_sheet=True,catalog_complete=False,catalog_listed_numbers=["B-001"])
    unit(repo,vid,oid,"A-001")
    unit(repo,vid,oid,"B-001")
    unit(repo,vid,oid,"B-002")
    result=run_catalog_titleblock(repo,vid)
    assert result["incomplete"]
    assert result["issues"] == []


def test_failed_catalog_and_unidentified_sheet_do_not_become_definitive_missing(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    unit(repo,vid,oid,"CAT-A",is_catalog_sheet=True,catalog_complete=True,catalog_listed_numbers=["A-001"])
    unit(repo,vid,oid,"unknown",extract_status="incomplete")
    result=run_catalog_titleblock(repo,vid)
    assert result["incomplete"] and result["issues"] == []


def test_changed_drawing_number_is_not_a_sheet_count_mismatch(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    unit(repo,vid,oid,"CAT-A",is_catalog_sheet=True,catalog_complete=True,catalog_listed_numbers=["A-001"])
    # This test asserts a real title/list discrepancy, with independent human confirmation.
    unit(repo,vid,oid,"B-001",manual_drawing_number_confirmed=True)
    result=run_catalog_titleblock(repo,vid)
    categories={issue["rule_result"]["category"] for issue in result["issues"]}
    assert "目录与图签不符" in categories
    assert "目录张数" not in categories
