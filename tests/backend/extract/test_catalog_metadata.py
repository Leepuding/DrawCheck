import json
from copy import deepcopy

from backend.extract import ExtractService, ExtractResult, PROMPT_VERSION
from backend.extract.schema import validate_extract_payload
from backend.models.openai_compat import CallResult
from tests.backend.extract.test_tiles import case


CATALOG = {"is_catalog_sheet":True,"catalog_listed_numbers":["VOL-01","S-001","S-002"],
    "catalog_volume_numbers":["VOL-01"], "catalog_complete":True,
    "catalog_bbox":{"x0":.1,"y0":.1,"x1":.9,"y1":.9}}


class CatalogClient:
    def __init__(self, incomplete=False, fail_at=None):
        self.calls=0
        self.incomplete=incomplete
        self.fail_at=fail_at
    def chat_completions(self, **kwargs):
        self.calls+=1
        if self.calls == self.fail_at:
            from backend.models.errors import ModelError
            raise ModelError("TIMEOUT", "controlled")
        catalog=deepcopy(CATALOG)
        catalog["catalog_complete"] = not self.incomplete
        return CallResult(json.dumps({"title_blocks":[],"facts":[],**catalog}), 10, 5, 200, "mock", "mock", 1, finish_reason="stop")


def test_schema_is_optional_for_old_and_ordinary_payloads():
    old=validate_extract_payload({"title_blocks":[],"facts":[]})
    assert "is_catalog_sheet" not in old
    ordinary=validate_extract_payload({"title_blocks":[],"facts":[],"is_catalog_sheet":False,"catalog_entries":"not required on a normal page"})
    assert ordinary["is_catalog_sheet"] is False and "catalog_entries" not in ordinary
    assert PROMPT_VERSION.endswith("v3")


def test_numeric_volume_ordinal_cannot_erase_real_directory_rows():
    payload=validate_extract_payload({"title_blocks":[],"facts":[],**CATALOG,
        "catalog_listed_numbers":["1","S-001"],"catalog_volume_numbers":[1]})
    assert payload["catalog_volume_numbers"] == []
    assert payload["catalog_volume_numbers_uncertain"] is True
    assert payload["catalog_listed_numbers"] == ["1","S-001"]
    assert payload["catalog_complete"] is True


def test_large_catalog_without_title_is_persisted_and_aggregate_cache_keeps_fields(case):
    store,version,unit,task=case
    client=CatalogClient()
    svc=ExtractService(store,use_mock=False,model_client=client,task_id=task["id"])
    first=svc.extract_unit(unit["id"])
    assert first.status == "ok" and first.title_blocks == []
    assert first.catalog["catalog_listed_numbers"] == ["S-001","S-002"]
    assert first.catalog["catalog_complete"] is True
    metadata=store.get("drawing_units",unit["id"])["metadata"]
    assert metadata["is_catalog_sheet"] and metadata["catalog_complete"]
    assert metadata["catalog_listed_numbers"] == ["S-001","S-002"]
    second=svc.extract_unit(unit["id"])
    assert second.cache_hit and second.model_calls == 0 and client.calls == 3
    assert second.as_dict()["catalog_listed_numbers"] == ["S-001","S-002"]


def test_partial_region_union_never_proves_complete_catalog(case):
    store,version,unit,task=case
    svc=ExtractService(store,use_mock=False,model_client=CatalogClient(incomplete=True))
    result=svc.extract_unit(unit["id"])
    assert result.status == "ok" and result.catalog["is_catalog_sheet"]
    assert result.catalog["catalog_complete"] is False
    assert store.get("drawing_units",unit["id"])["metadata"]["catalog_complete"] is False
    combined=svc._merge_catalogs([{**CATALOG,"catalog_listed_numbers":["S-001"],"catalog_complete":False},
        {**CATALOG,"catalog_listed_numbers":["S-002"],"catalog_complete":False}])
    assert combined["catalog_listed_numbers"] == ["S-001","S-002"] and not combined["catalog_complete"]


def test_failed_region_does_not_persist_complete_catalog(case):
    store,version,unit,task=case
    result=ExtractService(store,use_mock=False,model_client=CatalogClient(fail_at=3),format_retries=0).extract_unit(unit["id"])
    assert result.status == "incomplete"
    meta=store.get("drawing_units",unit["id"])["metadata"]
    assert meta.get("catalog_complete") is not True


def test_old_payload_does_not_erase_existing_manual_catalog_metadata(case):
    store,version,unit,task=case
    metadata={**unit["metadata"],"is_catalog_sheet":True,"catalog_listed_numbers":["MANUAL-01"],"catalog_complete":True}
    store._update("drawing_units",unit["id"],{"metadata_json":json.dumps(metadata)})
    result=ExtractResult(unit["id"],version["id"],"ok",PROMPT_VERSION,facts=[])
    ExtractService(store,use_mock=False,model_client=CatalogClient())._persist(result,unit)
    meta=store.get("drawing_units",unit["id"])["metadata"]
    assert meta["catalog_listed_numbers"] == ["MANUAL-01"] and meta["catalog_complete"]
