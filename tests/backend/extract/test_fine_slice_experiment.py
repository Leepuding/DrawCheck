import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image

from backend.extract.fine_slice_experiment import (
    SlicePolicy, plan_slices, run_experiment, summarize_readings,
    OVERVIEW_INSTRUCTION, DETAIL_INSTRUCTION,
)


@pytest.mark.parametrize("size", [(1, 1), (100, 50), (1024, 1024), (1025, 1025),
                                   (2482, 1755), (8192, 4096), (10, 2200)])
def test_deterministic_native_coverage_has_no_gaps(size):
    regions = plan_slices(*size)
    assert regions == plan_slices(*size)
    assert regions[0] == {"id": "overview", "box": [0, 0, *size], "kind": "overview"}
    details = regions[1:]
    assert len({tuple(row["box"]) for row in details}) == len(details)
    # Check every arrangement cell, not a sparse pixel sample. All possible gaps
    # have boundaries in these coordinate sets.
    xs = sorted({0, size[0]} | {value for r in details for value in (r["box"][0], r["box"][2])})
    ys = sorted({0, size[1]} | {value for r in details for value in (r["box"][1], r["box"][3])})
    for x0, x1 in zip(xs, xs[1:]):
        for y0, y1 in zip(ys, ys[1:]):
            assert any(a <= x0 < x1 <= c and b <= y0 < y1 <= d
                       for a, b, c, d in (r["box"] for r in details))
    assert all(0 <= a < c <= size[0] and 0 <= b < d <= size[1]
               and c-a <= 1024 and d-b <= 1024 for a, b, c, d in (r["box"] for r in details))


def test_pilot_call_counts_and_overlap():
    fine = plan_slices(2482, 1755, SlicePolicy(1024, 256))
    coarse = plan_slices(2482, 1755, SlicePolicy(2048, 256))
    assert len(fine) == 7 and len(coarse) == 3
    row = [r["box"] for r in fine[1:] if r["box"][1] == 0]
    assert row[0][2] - row[1][0] == 256
    assert row[-1][2] == 2482


@pytest.mark.parametrize("kwargs", [{"tile_size": 0}, {"tile_size": True}, {"overlap": -1},
                                   {"overlap": 1024}, {"overlap": .2}, {"version": ""}])
def test_bad_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        SlicePolicy(**kwargs)


def fact(text="基础板厚100mm，不可洞穿；锚入80mm", box=None):
    return {"text": text, "bbox": box or {"x0": .1, "y0": .2, "x1": .5, "y1": .6},
            "category": "尺寸", "numeric_value": None, "unit": "mm", "confidence": .9}


class Recognizer:
    def __init__(self, fail_at=None, invalid_at=None):
        self.requests = []
        self.fail_at, self.invalid_at = fail_at, invalid_at

    def __call__(self, request):
        self.requests.append(request)
        if len(self.requests) == self.fail_at:
            error = RuntimeError("controlled model timeout")
            error.model_calls = 1
            raise error
        raw = {"facts": [fact(), fact()], "title_blocks": [],
               "is_catalog_sheet": False, "unknown_context": "6kN水平 / 4kN竖向"}
        if len(self.requests) == self.invalid_at:
            raw["facts"] = "not an array"
        return raw, {"model_calls": 1, "latency_ms": 10}


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "S-or-5-must-not-be-repaired.png"
    Image.new("RGB", (200, 150), "white").save(path)
    return path


def execute(image, root, recognize, **kwargs):
    return run_experiment(image, root, recognize=recognize,
                          model_identity=kwargs.pop("model_identity", {"provider": "test", "model": "test"}),
                          policy=kwargs.pop("policy", SlicePolicy(128, 32)), **kwargs)


def test_callback_mapping_lossless_raw_context_and_strict_resume(image, tmp_path):
    client = Recognizer()
    root = tmp_path / "experiment"
    before = hashlib.sha256(image.read_bytes()).hexdigest()
    result = execute(image, root, client)
    assert result["status"] == "completed" and result["quality_status"] == "unassessed"
    assert result["model_calls"] == len(client.requests) == 5
    assert len(result["readings"]["facts"]) == 10  # no same-text dedup, including overview
    assert all(r["raw_payload"]["unknown_context"] == "6kN水平 / 4kN竖向" for r in result["regions"])
    first = result["readings"]["facts"][2]
    assert first["local_bbox"] == fact()["bbox"]
    assert first["bbox"] == {"x0": .064, "x1": .32, "y0": 128*.2/150, "y1": 128*.6/150}
    assert first["region_ids"] == ["region-1"] and first["pixel_box"] == [0, 0, 128, 128]
    assert client.requests[0].image_path == image
    assert client.requests[0].image_sha256 == before
    assert client.requests[0].instruction == OVERVIEW_INSTRUCTION
    assert all(r.instruction == DETAIL_INSTRUCTION for r in client.requests[1:])
    for request in client.requests[1:]:
        with Image.open(request.image_path) as crop:
            a, b, c, d = request.pixel_box
            assert crop.size == (c-a, d-b)
    again = execute(image, root, client)
    assert again["status"] == "completed" and again["model_calls"] == 0
    assert len(client.requests) == 5 and all(r["cache_hit"] for r in again["regions"])
    assert hashlib.sha256(image.read_bytes()).hexdigest() == before
    assert json.loads((root/"manifest.json").read_text())["readings"] == again["readings"]


def test_failure_stays_partial_and_resume_only_missing(image, tmp_path):
    client = Recognizer(fail_at=3)
    root = tmp_path / "out"
    failed = execute(image, root, client)
    assert failed["status"] == "partial" and failed["completed_regions"] == 2
    assert failed["model_calls"] == 3 and len(client.requests) == 3
    assert failed["incomplete_region_ids"] == ["region-2", "region-3", "region-4"]
    assert failed["regions"][2]["state"] == "failed"
    assert failed["quality_status"] == "unassessed"
    assert failed["views"]["overview_baseline"]["status"] == "completed"
    assert failed["views"]["fine_candidate"]["status"] == "partial"
    resumed = execute(image, root, client)
    assert resumed["status"] == "completed" and resumed["model_calls"] == 3
    assert len(client.requests) == 6


def test_shared_overview_baseline_then_fine_without_second_overview_call(image, tmp_path):
    client = Recognizer()
    root = tmp_path/"out"
    baseline = execute(image, root, client, overview_only=True)
    assert baseline["status"] == "partial" and baseline["execution_scope"] == "overview_only"
    assert baseline["model_calls"] == len(client.requests) == 1
    assert baseline["views"]["overview_baseline"]["status"] == "completed"
    assert baseline["views"]["fine_candidate"]["status"] == "partial"
    full = execute(image, root, client)
    assert full["experiment_key"] == baseline["experiment_key"]
    assert full["status"] == "completed" and full["model_calls"] == 4
    assert len(client.requests) == 5 and full["regions"][0]["cache_hit"] is True
    assert full["views"]["overview_baseline"]["readings"] == baseline["views"]["overview_baseline"]["readings"]
    assert full["views"]["overview_baseline"]["quality_status"] == "unassessed"


def test_malformed_response_preserved_but_not_cached_as_success(image, tmp_path):
    client = Recognizer(invalid_at=2)
    result = execute(image, tmp_path/"out", client)
    assert result["status"] == "partial" and result["model_calls"] == 2
    assert result["regions"][1]["raw_payload"]["facts"] == "not an array"
    assert len(list((tmp_path/"out"/"cache").glob("*.json"))) == 1


def test_nonfinite_malformed_response_remains_inspectable_partial_json(image, tmp_path):
    def recognize(request):
        row = fact()
        row["bbox"]["x0"] = float("nan")
        return {"facts": [row]}, {"model_calls": 1}
    result = execute(image, tmp_path/"out", recognize)
    assert result["status"] == "partial" and result["completed_regions"] == 0
    assert "NaN" in result["regions"][0]["raw_payload"]["raw_text"]
    text = (tmp_path/"out"/"manifest.json").read_text()
    assert json.loads(text)["regions"][0]["state"] == "failed"


@pytest.mark.parametrize("change", ["policy", "strategy_version", "model", "prompt_version", "system_prompt",
                                    "user_prompt", "instruction_suffix", "source"])
def test_cache_isolates_every_strategy_prompt_model_and_source_input(image, tmp_path, change):
    client = Recognizer()
    root = tmp_path/"out"
    base = execute(image, root, client)
    kwargs = {}
    if change == "policy":
        kwargs["policy"] = SlicePolicy(128, 40)
    elif change == "strategy_version":
        kwargs["policy"] = SlicePolicy(128, 32, "new-exp-version")
    elif change == "model":
        kwargs["model_identity"] = {"provider": "other", "model": "test"}
    elif change == "source":
        with Image.open(image) as source:
            source.putpixel((199, 149), (0, 0, 0))
            source.save(image)
    else:
        kwargs[change] = "changed"
    result = execute(image, root, client, **kwargs)
    assert result["experiment_key"] != base["experiment_key"]
    assert result["model_calls"] == result["total_regions"]
    assert not any(row["cache_hit"] for row in result["regions"])


def test_corrupt_cache_or_forged_identity_is_not_success(image, tmp_path):
    client = Recognizer()
    root = tmp_path/"out"
    first = execute(image, root, client)
    cache_path = root/"cache"/f"{first['regions'][0]['cache_key']}.json"
    cached = json.loads(cache_path.read_text())
    cached["identity"]["source_image_sha256"] = "forged"
    cache_path.write_text(json.dumps(cached))
    result = execute(image, root, client)
    assert result["status"] == "partial" and result["model_calls"] == 0
    assert result["regions"][0]["state"] == "failed"
    assert result["regions"][0]["error_code"] == "CACHE_IDENTITY_MISMATCH"
    cache_path.write_text("invalid json")
    assert execute(image, root, client)["status"] == "partial"


@pytest.mark.parametrize("tamper", ["legal_payload", "missing_digest"])
def test_valid_json_cache_payload_tampering_is_rejected(image, tmp_path, tamper):
    client = Recognizer()
    root = tmp_path/"out"
    first = execute(image, root, client)
    cache_path = root/"cache"/f"{first['regions'][0]['cache_key']}.json"
    cached = json.loads(cache_path.read_text())
    assert len(cached["raw_payload_sha256"]) == 64
    if tamper == "legal_payload":
        cached["raw_payload"]["facts"][0]["text"] = "合法JSON，但数值被篡改为900mm"
    else:
        cached.pop("raw_payload_sha256")
    cache_path.write_text(json.dumps(cached))
    result = execute(image, root, client)
    assert result["status"] == "partial" and result["completed_regions"] == 0
    assert result["model_calls"] == 0 and len(client.requests) == 5
    assert result["regions"][0]["error_code"] == "CACHE_PAYLOAD_DIGEST_MISMATCH"


def test_callback_exception_credentials_never_reach_manifest(image, tmp_path):
    secret = "sk-private-token-DO-NOT-SAVE"
    def recognize(request):
        raise RuntimeError(f"HTTP headers Authorization: Bearer {secret}; https://user:password@example.test")
    result = execute(image, tmp_path/"out", recognize)
    assert result["status"] == "partial"
    assert result["regions"][0]["error"] == "RuntimeError"
    saved = (tmp_path/"out"/"manifest.json").read_text()
    assert secret not in saved and "user:password" not in saved


def test_dryrun_materializes_native_crops_without_call_or_quality_claim(image, tmp_path):
    client = Recognizer()
    result = execute(image, tmp_path/"out", client, dry_run=True)
    assert result["status"] == "partial" and result["quality_status"] == "unassessed"
    assert result["completed_regions"] == result["model_calls"] == len(client.requests) == 0
    assert all(row["state"] == "planned" and Path(row["image_path"]).is_file() for row in result["regions"])
    assert result["readings"]["facts"] == []


def test_title_disagreement_and_repeated_physical_rows_are_retained():
    title = {"drawing_number": "T0105-03", "bbox": {"x0": .8, "y0": .8, "x1": 1, "y1": 1}}
    region = {"id": "overview", "kind": "overview", "box": [0, 0, 100, 100], "state": "completed",
              "payload": {"facts": [fact("250"), fact("250")], "title_blocks": [title]}}
    other = deepcopy(region)
    other.update(id="region-1", kind="detail")
    other["payload"]["title_blocks"][0]["drawing_number"] = "11#"
    result = summarize_readings([region, other], (100, 100))
    assert [r["drawing_number"] for r in result["title_blocks"]] == ["T0105-03", "11#"]
    assert len(result["title_conflict_candidates"]) == 1 and len(result["facts"]) == 4


def test_catalog_local_coordinates_and_rows_retained():
    entry = {"drawing_number": "09", "bbox": fact()["bbox"]}
    region = {"id": "region-1", "kind": "detail", "box": [50, 0, 100, 100], "state": "completed",
              "payload": {"facts": [], "title_blocks": [], "is_catalog_sheet": True,
                          "catalog_complete": False, "catalog_bbox": fact()["bbox"],
                          "catalog_entries": [entry, deepcopy(entry)]}}
    result = summarize_readings([region], (100, 100))
    assert len(result["catalog_entries"]) == 2
    assert result["catalogs"][0]["catalog_bbox"]["x0"] == .55
    assert result["catalogs"][0]["catalog_complete"] is False
