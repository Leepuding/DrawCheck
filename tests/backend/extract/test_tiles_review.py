"""Independent review regressions: inference caches must agree with persisted evidence."""
import json
from copy import deepcopy

from backend.extract import ExtractService
from backend.extract.service import ExtractResult
from backend.extract.prompt import PROMPT_VERSION
from backend.store import Evidence, BBox, Source
from tests.backend.extract.test_tiles import case, Client, fact


class ValueClient(Client):
    def __init__(self, value):
        super().__init__()
        self.value = value
    def chat_completions(self, **kwargs):
        result = super().chat_completions(**kwargs)
        data = json.loads(result.content)
        for row in data["facts"]:
            row["text"] = row["numeric_value"] = self.value
        result.content = json.dumps(data)
        return result


def new_unit(store, original_id, name):
    project = store.create_project(name)
    version = store.create_version(project["id"], "v1")
    original = store.import_original(version["id"], store.original_path(original_id))["record"]
    unit = store.create_unit(version["id"], original["id"], kind="pdf_page", ordinal=1, name=name,
        metadata={"review_relative_path":"review.png"})
    return version, unit


def test_copied_unit_does_not_keep_model_a_evidence_when_model_b_cache_exists(case):
    store, version, unit, task = case
    a = ExtractService(store, use_mock=False, model_client=ValueClient("100"), provider="custom", model_name="A")
    assert a.extract_unit(unit["id"]).status == "ok"
    before = store.get("drawing_units", unit["id"])
    copied_version, copied = new_unit(store, unit["original_id"], "copied")
    metadata = deepcopy(before["metadata"])
    metadata["copied_from_unit_id"] = unit["id"]
    annotations = {}
    for old in store.list_records("facts", version_id=version["id"]):
        row = store.add_fact(copied_version["id"], Evidence(copied["id"], Source.VISION, old["text"],
            BBox(**{k:old[k] for k in ("x0","y0","x1","y1")})), numeric_value=old["numeric_value"], unit=old["unit"])
        annotations[row["id"]] = metadata["fact_annotations"][old["id"]]
    metadata["fact_annotations"] = annotations
    store._update("drawing_units", copied["id"], {"metadata_json":json.dumps(metadata)})
    _, unrelated = new_unit(store, unit["original_id"], "model-B-cache")
    client = ValueClient("200")
    b = ExtractService(store, use_mock=False, model_client=client, provider="custom", model_name="B")
    assert b.extract_unit(unrelated["id"]).status == "ok"
    result = b.extract_unit(copied["id"])
    assert result.cache_hit and result.model_calls == 0 and client.calls == 3
    assert {f["text"] for f in result.facts} == {"200"}
    assert {f["text"] for f in store.list_records("facts", version_id=copied_version["id"])} == {"200"}
    copied_meta = store.get("drawing_units", copied["id"])["metadata"]
    assert copied_meta["extract_cache_identity"] == result.cache_identity
    assert copied_meta["extract_coverage"]["completed_regions"] == copied_meta["extract_coverage"]["total_regions"] == 3


def test_same_text_location_with_different_object_identity_is_not_collapsed_on_persist(case):
    store,version,unit,task = case
    facts = [fact(object_id="F1"), fact(object_id="F2")]
    service = ExtractService(store, use_mock=False, model_client=Client())
    result = ExtractResult(unit["id"], version["id"], "ok", PROMPT_VERSION, facts=facts)
    service._persist(result, unit)
    assert len(store.list_records("facts", version_id=version["id"])) == 2
    annotations = store.get("drawing_units", unit["id"])["metadata"]["fact_annotations"]
    assert {row["object_id"] for row in annotations.values()} == {"F1","F2"}


def test_title_in_local_region_can_recover_empty_overview_and_maps_to_whole_sheet(case):
    store,version,unit,task = case
    class LocalTitleClient(Client):
        def chat_completions(self, **kwargs):
            result = super().chat_completions(**kwargs)
            data = json.loads(result.content)
            data["title_blocks"] = [] if self.calls < 3 else [{"drawing_number":"S-099", "name":"恢复的图签",
                "bbox":{"x0":.8,"y0":.8,"x1":.9,"y1":.9}, "confidence":.9}]
            result.content = json.dumps(data)
            return result
    result = ExtractService(store, use_mock=False, model_client=LocalTitleClient()).extract_unit(unit["id"])
    assert result.status == "ok" and len(result.title_blocks) == 1
    title = result.title_blocks[0]
    assert title["drawing_number"] == "S-099"
    assert abs(title["bbox"]["x0"] - (952+.8*2048)/3000) < 1e-9
    assert title["bbox"]["y0"] == .8


def test_all_regions_missing_title_is_incomplete_and_does_not_persist_facts(case):
    store,version,unit,task = case
    class NoTitleClient(Client):
        def chat_completions(self, **kwargs):
            result = super().chat_completions(**kwargs)
            data = json.loads(result.content)
            data["title_blocks"] = []
            result.content = json.dumps(data)
            return result
    result = ExtractService(store, use_mock=False, model_client=NoTitleClient()).extract_unit(unit["id"])
    assert result.status == "incomplete" and "图签" in result.incomplete_reason
    assert store.list_records("facts", version_id=version["id"]) == []
    assert store.get("drawing_units", unit["id"])["metadata"]["extract_status"] == "incomplete"
