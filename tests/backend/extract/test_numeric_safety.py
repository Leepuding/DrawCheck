"""A multi-value field cannot be guessed into a scalar, including old cached payloads."""
import hashlib
import json

import pytest

from backend.extract import ExtractService, ExtractResult, PROMPT_VERSION
from backend.extract.schema import validate_extract_payload
from backend.extract.tiles import MERGE_VERSION, STRATEGY
from backend.extract.service import POSITION_VERSION
from tests.backend.extract.test_tiles import case, Client, fact


@pytest.mark.parametrize("numeric", [[.31,.35],[.85,20],[10],"10~20","1/2","NaN","Infinity",float("nan"),True,{}])
def test_non_scalar_or_non_finite_numeric_is_pending_without_changing_text(numeric):
    payload=validate_extract_payload({"facts":[{**fact("雪压0.31/0.35"),"numeric_value":numeric}]})
    result=payload["facts"][0]
    assert result["text"] == "雪压0.31/0.35"
    assert result["numeric_value"] is None and result["unclear"]
    assert result["numeric_value_invalid"]


@pytest.mark.parametrize("numeric,expected", [(10,"10"),(.31,"0.31"),(" -12.50 ","-12.50"),(None,None)])
def test_scalar_decimal_or_no_numeric_remains_valid(numeric,expected):
    payload=validate_extract_payload({"facts":[{**fact("原文"),"numeric_value":numeric}]})
    result=payload["facts"][0]
    assert result["numeric_value"] == expected and not result["unclear"]


def test_aggregate_cache_array_numeric_is_normalized_before_store(case):
    store,version,unit,task=case
    client=Client()
    service=ExtractService(store,use_mock=False,model_client=client)
    digest=hashlib.sha256((store.root/"review.png").read_bytes()).hexdigest()
    role=service.cache_role+":"+STRATEGY
    old_fact={**fact("25/50年雪压0.31/0.35"),"numeric_value":[.31,.35]}
    service.cache.set(digest,PROMPT_VERSION,role,{"payload":{"title_blocks":[],"facts":[old_fact]},
        "merge_version":MERGE_VERSION,"position_version":POSITION_VERSION,"persisted_unit_ids":[]})
    result=service.extract_unit(unit["id"])
    assert result.cache_hit and result.model_calls == 0 and client.calls == 0
    rows=store.list_records("facts",version_id=version["id"])
    assert len(rows) == 1 and rows[0]["numeric_value"] is None
    assert rows[0]["text"] == old_fact["text"]
    pending=store.list_records("pending_confirmations",version_id=version["id"])
    assert len(pending) == 1 and pending[0]["text"] == old_fact["text"]
    annotation=store.get("drawing_units",unit["id"])["metadata"]["fact_annotations"][rows[0]["id"]]
    assert annotation["numeric_value_invalid"] and annotation["unclear"]


def test_direct_result_persistence_defends_against_singleton_list(case):
    store,version,unit,task=case
    result=ExtractResult(unit["id"],version["id"],"ok",PROMPT_VERSION,facts=[{**fact("M10螺栓"),"numeric_value":[10]}])
    ExtractService(store,use_mock=False,model_client=Client())._persist(result,unit)
    row=store.list_records("facts",version_id=version["id"])[0]
    assert row["numeric_value"] is None and row["text"] == "M10螺栓"
    assert len(store.list_records("pending_confirmations",version_id=version["id"])) == 1
