"""Editing a name/specialty is not confirmation of the drawing-number field."""
import json

import pytest

from backend.evidence_selection import usable_title_identity
from backend.extract import ExtractResult, ExtractService, PROMPT_VERSION
from backend.rules.catalog_titleblock import run_catalog_titleblock
from backend.store import Source
from backend.title_identity import MANUAL_NUMBER_FIELD, has_manual_number
from tests.backend.catalog.test_title_source_resolution import title_case, add, pair, block, apply


@pytest.mark.parametrize("change",[{"name":"human name"},{"specialty":"human specialty"}])
def test_name_or_specialty_only_never_confirms_uncertain_number(title_case,change):
    store,version,unit,catalog=title_case
    store.update_unit_directory(unit["id"],name=unit["name"],drawing_number="WRONG-015")
    apply(title_case,block(confidence=.1,unclear=True))
    catalog.rename(unit_id=unit["id"],**change)
    current=store.get("drawing_units",unit["id"])
    assert current["metadata"][MANUAL_NUMBER_FIELD] is False and not has_manual_number(current)
    assert current["metadata"]["title_identity_uncertain"] and not usable_title_identity(current)
    assert catalog.list_catalog(version["id"])[0]["drawing_number"] == "JOB-815-X-04"
    result=ExtractResult(unit["id"],version["id"],"ok",PROMPT_VERSION,title_blocks=[block(confidence=.1,unclear=True)])
    ExtractService(store,use_mock=False)._persist(result,current)
    row,current=apply(title_case,block(confidence=.1,unclear=True))
    assert current["drawing_number"] == "WRONG-015" and current["metadata"]["title_identity_uncertain"]
    assert row["title_identity_uncertain"] and row["manual_correction"]
    assert row["title_number_resolution"]["selected_source"] == Source.VISION.value
    assert run_catalog_titleblock(store,version["id"],expected_prefix="EXPECTED")["issues"] == []
    assert store.list_records("pending_confirmations",version_id=version["id"])


@pytest.mark.parametrize("change",[{"name":"human name"},{"specialty":"human specialty"}])
def test_name_only_does_not_freeze_wrong_number_and_cad_can_still_resolve_it(title_case,change):
    store,version,unit,catalog=title_case
    apply(title_case)
    catalog.rename(unit_id=unit["id"],**change)
    pair(title_case,"REAL-81S-X-04")
    row,current=apply(title_case)
    assert row["drawing_number"] == current["drawing_number"] == "REAL-81S-X-04"
    assert current["metadata"][MANUAL_NUMBER_FIELD] is False and not has_manual_number(current)
    assert row["title_number_resolution"]["selected_source"] == Source.CAD_TEXT.value
    if "name" in change:
        assert row["name"] == current["name"] == change["name"]
    result=run_catalog_titleblock(store,version["id"],expected_prefix="OTHER")
    issue=next(i for i in result["issues"] if i["rule_result"]["category"] == "图签编号前缀")
    assert issue["left_source"] == Source.CAD_TEXT.value


def test_name_edit_preserves_existing_cad_provenance_instead_of_turning_it_user(title_case):
    store,version,unit,catalog=title_case
    _,value=pair(title_case,"CAD-007")
    apply(title_case)
    catalog.rename(unit_id=unit["id"],name="human name")
    current=store.get("drawing_units",unit["id"])
    resolution=current["metadata"]["title_number_resolution"]
    assert resolution["selected_source"] == Source.CAD_TEXT.value
    assert resolution["selected_evidence"]["fact_id"] == value["id"]
    assert not has_manual_number(current)


def test_explicit_number_confirmation_survives_later_name_edits_and_conflicting_cad(title_case):
    store,version,unit,catalog=title_case
    apply(title_case,block(confidence=.1,unclear=True))
    renamed=catalog.rename(unit_id=unit["id"],drawing_number="USER-001")
    assert not renamed["title_identity_uncertain"]
    catalog.rename(unit_id=unit["id"],name="later name")
    pair(title_case,"CAD-007")
    row,current=apply(title_case,block(confidence=.1,unclear=True))
    assert current["drawing_number"] == row["drawing_number"] == "USER-001"
    assert current["metadata"][MANUAL_NUMBER_FIELD] is True and has_manual_number(current)
    assert row["title_number_resolution"]["manual_confirmation_mode"] == "explicit"
    assert not current["metadata"]["title_identity_uncertain"]


def test_explicit_blank_is_not_restored_by_extraction_catalog_or_stale_catalog_row(title_case):
    store,version,unit,catalog=title_case
    pair(title_case,"CAD-007")
    apply(title_case)
    cleared=catalog.rename(unit_id=unit["id"],drawing_number="")
    assert cleared["title_identity_uncertain"]
    current=store.get("drawing_units",unit["id"])
    assert current["metadata"][MANUAL_NUMBER_FIELD] is True and current["metadata"]["title_identity_uncertain"]
    result=ExtractResult(unit["id"],version["id"],"ok",PROMPT_VERSION,title_blocks=[block()])
    ExtractService(store,use_mock=False)._persist(result,current)
    row,current=apply(title_case)
    assert current["drawing_number"] == row["drawing_number"] == ""
    assert current["metadata"]["title_drawing_number"] == "" and current["metadata"]["title_identity_uncertain"]
    assert row["title_identity_uncertain"] and not usable_title_identity(current)
    state=catalog._load_state()
    state["versions"][version["id"]][0]["drawing_number"]="STALE-007"
    catalog._save_state(state)
    assert catalog.list_catalog(version["id"])[0]["drawing_number"] == ""
    assert run_catalog_titleblock(store,version["id"],expected_prefix="STALE")["issues"] == []


@pytest.mark.parametrize("uncertainty",[None,False])
def test_legacy_stable_manual_number_is_compatible_but_not_claimed_explicit(title_case,uncertainty):
    store,version,unit,catalog=title_case
    meta={"manual_correction":True}
    if uncertainty is not None:
        meta["title_identity_uncertain"]=uncertainty
    store.update_unit_directory(unit["id"],name="legacy name",drawing_number="LEGACY-001")
    store._update("drawing_units",unit["id"],{"metadata_json":json.dumps(meta)})
    pair(title_case,"CAD-007")
    row,current=apply(title_case,block(confidence=.1,unclear=True))
    assert row["drawing_number"] == current["drawing_number"] == "LEGACY-001"
    assert MANUAL_NUMBER_FIELD not in current["metadata"]
    assert row["title_number_resolution"]["manual_confirmation_mode"] == "legacy_unverified_field"
    result=run_catalog_titleblock(store,version["id"],expected_prefix="OTHER")
    issue=next(i for i in result["issues"] if i["rule_result"]["category"] == "图签编号前缀")
    assert issue["rule_result"]["manual_number_confirmation_mode"] == "legacy_unverified_field"


@pytest.mark.parametrize("via_extract",[False,True])
def test_legacy_uncertain_flag_cannot_become_number_confirmation(title_case,via_extract):
    store,version,unit,catalog=title_case
    store.update_unit_directory(unit["id"],name="legacy name",drawing_number="LEGACY-WRONG")
    store._update("drawing_units",unit["id"],{"metadata_json":json.dumps({"manual_correction":True,"title_identity_uncertain":True})})
    current=store.get("drawing_units",unit["id"])
    if via_extract:
        result=ExtractResult(unit["id"],version["id"],"ok",PROMPT_VERSION,title_blocks=[block()])
        ExtractService(store,use_mock=False)._persist(result,current)
        assert store.get("drawing_units",unit["id"])["metadata"][MANUAL_NUMBER_FIELD] is False
    else:
        catalog.rename(unit_id=unit["id"],name="name only")
        assert store.get("drawing_units",unit["id"])["metadata"]["title_identity_uncertain"]
    pair(title_case,"CAD-007")
    row,current=apply(title_case)
    assert row["drawing_number"] == current["drawing_number"] == "CAD-007"
    assert current["metadata"][MANUAL_NUMBER_FIELD] is False and not has_manual_number(current)
    assert row["title_number_resolution"]["selected_source"] == Source.CAD_TEXT.value


def test_revision_does_not_match_name_edited_pdf_page_ordinals_as_confirmed_numbers(title_case,tmp_path):
    from backend.catalog import CatalogService
    from backend.revisions import RevisionService
    store=title_case[0]
    project=store.create_project("revision placeholder boundary")
    old=store.create_version(project["id"],"old")
    prior=[]
    for name in ("first.pdf","second.pdf"):
        source=tmp_path/name; source.write_bytes(b"%PDF-old-"+name.encode())
        original=store.import_original(old["id"],source)["record"]
        unit=store.create_unit(old["id"],original["id"],kind="pdf_page",ordinal=1,name=name,drawing_number="1",
            metadata={"title_identity_uncertain":True})
        CatalogService(store).rename(unit_id=unit["id"],name="name edit only")
        prior.append(unit)
    service=RevisionService(store)
    draft=service.create_revision(old["id"])
    incoming=[]
    for name in ("first.pdf","second.pdf"):
        source=tmp_path/name; source.write_bytes(b"%PDF-new-"+name.encode())
        original=store.import_original(draft["version_id"],source)["record"]
        unit=store.create_unit(draft["version_id"],original["id"],kind="pdf_page",ordinal=1,name=name,drawing_number="1",
            metadata={"title_identity_uncertain":True})
        CatalogService(store).rename(unit_id=unit["id"],name="another name edit")
        incoming.append(unit)
    plan=service.prepare(draft["version_id"])
    assert plan["old_to_new"] == {old_unit["id"]:new_unit["id"] for old_unit,new_unit in zip(prior,incoming)}
    assert set(plan["changed_unit_ids"]) == {unit["id"] for unit in incoming}


def test_legacy_catalog_row_cannot_override_current_uncertain_field_state(title_case):
    store,version,unit,catalog=title_case
    apply(title_case)
    catalog.rename(unit_id=unit["id"],name="legacy edited name")
    current=store.get("drawing_units",unit["id"])
    meta=current["metadata"]
    meta.pop(MANUAL_NUMBER_FIELD,None)
    meta["title_identity_uncertain"]=True
    store._update("drawing_units",unit["id"],{"metadata_json":json.dumps(meta)})
    state=catalog._load_state()
    state["versions"][version["id"]][0].pop(MANUAL_NUMBER_FIELD,None)
    state["versions"][version["id"]][0]["title_identity_uncertain"]=False
    catalog._save_state(state)
    pair(title_case,"CAD-007")
    row,current=apply(title_case)
    assert row["drawing_number"] == current["drawing_number"] == "CAD-007"
    assert row[MANUAL_NUMBER_FIELD] is False and current["metadata"][MANUAL_NUMBER_FIELD] is False


@pytest.mark.parametrize("change,expected",[({"drawing_number":"USER-SECOND"},"USER-SECOND"),({"name":"human second name"},"NEXT-SECOND")])
def test_secondary_catalog_row_also_distinguishes_name_edit_from_number_confirmation(title_case,change,expected):
    store,version,unit,catalog=title_case
    first=ExtractResult(unit["id"],version["id"],"ok",PROMPT_VERSION,
        title_blocks=[block(drawing_number="FIRST"),block(drawing_number="SECOND")])
    rows=catalog.apply_extract_result(first)
    catalog.rename(row_id=rows[1]["row_id"],**change)
    second=ExtractResult(unit["id"],version["id"],"ok",PROMPT_VERSION,
        title_blocks=[block(drawing_number="FIRST"),block(drawing_number="NEXT-SECOND")])
    rows=catalog.apply_extract_result(second)
    row=next(r for r in rows if r["frame_index"]==1)
    assert row["drawing_number"] == expected
    if "drawing_number" in change:
        assert row["title_number_resolution"]["selected_source"] == Source.USER.value
        assert row["title_number_resolution"]["manual_confirmation_mode"] == "explicit"
    else:
        assert row[MANUAL_NUMBER_FIELD] is False
        assert row["title_number_resolution"]["selected_source"] == Source.VISION.value
        assert row["name"] == "human second name"
