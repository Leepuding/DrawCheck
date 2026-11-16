"""Opt-in, file-only recognition experiment; never persists production evidence.

Compare policies with the SAME callback/model/prompts.  A completed manifest
means every planned response passed structural validation, not quality acceptance.
All readings (including overview facts and overlapping duplicates) are retained.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .bbox_util import clamp_bbox
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from .schema import validate_extract_payload
from .tiles import map_fact, overlap_fraction


EXPERIMENT_VERSION = "fine-slice-evidence-v1"


class _CacheIntegrityError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _safe_error(exc: Exception) -> dict[str, str]:
    # Exception messages can contain URLs, request headers or credential values.
    # Keep a bounded taxonomy only; callback-specific diagnostics live in its
    # separately sanitized ledger.
    name = type(exc).__name__
    allowed_types = {"ValueError", "TypeError", "KeyError", "OSError", "RuntimeError",
                     "JSONDecodeError", "ExtractError", "PilotError", "_CacheIntegrityError"}
    result = {"error": name if name in allowed_types else "RecognitionError"}
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in {"CACHE_IDENTITY_MISMATCH", "CACHE_PAYLOAD_DIGEST_MISMATCH",
                "EXTRACT_BAD_FORMAT", "EXTRACT_INCOMPLETE", "EXTRACT_NO_IMAGE",
                "EXTRACT_CACHE_ERROR", "EXTRACT_REPLAY_MISS"}:
        result["error_code"] = code
    return result
# The paired baseline is one generic whole-page response. The candidate reuses
# exactly that response and adds fine local observations. The local instruction
# is an explicit necessary coordinate/identity difference, recorded in identity.
OVERVIEW_INSTRUCTION = ""
DETAIL_INSTRUCTION = (
    "本次输入是图纸的局部裁剪区域。bbox必须是当前输入图片内的0~1归一化坐标，不是整张图坐标。"
    "仅在局部可见完整图签时提供title_blocks，不能凭空推断整图身份。"
    "只凭当前区域内明确编号、轴线和位置确认对象；缺乏身份依据时identity_confirmed=false。"
)


@dataclass(frozen=True)
class SlicePolicy:
    tile_size: int = 1024
    overlap: int = 256
    version: str = EXPERIMENT_VERSION

    def __post_init__(self):
        if (type(self.tile_size) is not int or type(self.overlap) is not int or
                self.tile_size <= 0 or not 0 <= self.overlap < self.tile_size):
            raise ValueError("tile_size must be positive and 0 <= overlap < tile_size")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("policy version must be nonempty")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _image_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_slices(width: int, height: int, policy: SlicePolicy = SlicePolicy()) -> list[dict]:
    """Deterministic row-major, native-resolution coverage, including small pages."""
    if any(type(value) is not int or value <= 0 for value in (width, height)):
        raise ValueError("image dimensions must be positive integers")

    def starts(length):
        last = max(0, length - policy.tile_size)
        positions = list(range(0, last + 1, policy.tile_size - policy.overlap))
        if positions[-1] != last:
            positions.append(last)
        return positions

    regions = [{"id": "overview", "box": [0, 0, width, height], "kind": "overview"}]
    for y in starts(height):
        for x in starts(width):
            regions.append({"id": f"region-{len(regions)}", "kind": "detail",
                            "box": [x, y, min(x + policy.tile_size, width),
                                    min(y + policy.tile_size, height)]})
    return regions


@dataclass(frozen=True)
class SliceRequest:
    region_id: str
    kind: str
    image_path: Path
    image_sha256: str
    source_image_sha256: str
    pixel_box: tuple[int, int, int, int]
    source_size: tuple[int, int]
    instruction: str
    prompt_version: str
    system_prompt: str
    user_prompt: str
    cache_key: str


def _prepare(raw: dict) -> dict:
    payload = validate_extract_payload(raw)
    rows = payload["facts"] + payload["title_blocks"] + payload.get("catalog_entries", [])
    for row in rows:
        box, changed, _ = clamp_bbox(row.get("bbox"))
        row["bbox"] = box
        if changed:
            row.update(unclear=True, bbox_clamped=True)
    if payload.get("catalog_bbox") is not None:
        box, changed, _ = clamp_bbox(payload["catalog_bbox"])
        payload["catalog_bbox"] = box
        if changed:
            payload["catalog_complete"] = False
    return payload


def summarize_readings(regions: list[dict], size: tuple[int, int]) -> dict:
    """Lossless evidence inventory, not semantic synthesis or a quality verdict.

    Even equal text in equal boxes is kept: a table can contain repeated physical
    rows, and coarse overview boxes cannot prove those readings are duplicates.
    """
    result = {"facts": [], "title_blocks": [], "catalog_entries": [], "catalogs": []}
    for region in regions:
        if region.get("state") != "completed":
            continue
        payload = region["payload"]
        for key in ("facts", "title_blocks", "catalog_entries"):
            for index, row in enumerate(payload.get(key, [])):
                mapped = map_fact(row, region["box"], size, region["id"])
                mapped.update(evidence_id=f"{region['id']}:{key}:{index}",
                              local_bbox=deepcopy(row["bbox"]),
                              pixel_box=list(region["box"]), region_kind=region["kind"])
                result[key].append(mapped)
        if "is_catalog_sheet" in payload:
            catalog = {key: deepcopy(value) for key, value in payload.items()
                       if key.startswith("catalog_") or key == "is_catalog_sheet"}
            if catalog.get("catalog_bbox") is not None:
                catalog["catalog_bbox"] = map_fact({"bbox": catalog["catalog_bbox"]},
                                                   region["box"], size, region["id"])["bbox"]
            catalog["catalog_entries"] = [entry for entry in result["catalog_entries"]
                                           if entry["region_ids"] == [region["id"]]]
            catalog["region_id"] = region["id"]
            result["catalogs"].append(catalog)
    # Disagreeing title candidates are reported, never silently superseded by
    # a higher-confidence overview or repaired by filename / S-to-5 guesses.
    conflicts = []
    for index, left in enumerate(result["title_blocks"]):
        for right in result["title_blocks"][index + 1:]:
            if (left["region_ids"] != right["region_ids"] and
                    left.get("drawing_number") and right.get("drawing_number") and
                    left["drawing_number"] != right["drawing_number"] and
                    overlap_fraction(left["bbox"], right["bbox"]) >= .7):
                conflicts.append([left["evidence_id"], right["evidence_id"]])
    result["title_conflict_candidates"] = conflicts
    result["deduplication"] = "none; all source readings retained"
    return result


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _safe_record(value: Any) -> Any:
    """Keep malformed callback data inspectable without corrupting the manifest."""
    try:
        json.dumps(value, allow_nan=False)
        return deepcopy(value)
    except (ValueError, TypeError):
        return {"serialization": "invalid JSON retained as text; not accepted evidence",
                "raw_text": json.dumps(value, ensure_ascii=False, default=repr)}


def paired_views(manifest: dict) -> dict:
    """Two evidence views over the SAME responses; never new calls/cache claims.

    Baseline can finish even if a later detail fails. Candidate remains partial
    until every region is complete. Both quality verdicts stay unassessed.
    """
    size = tuple(manifest["image_size"])
    result = {}
    for name, regions in (("overview_baseline", manifest["regions"][:1]),
                          ("fine_candidate", manifest["regions"])):
        completed = sum(row.get("state") == "completed" for row in regions)
        result[name] = {
            "source": "shared manifest responses; no additional inference",
            "status": "completed" if regions and completed == len(regions) else "partial",
            "quality_status": "unassessed", "planned_regions": len(regions),
            "completed_regions": completed, "region_ids": [row["id"] for row in regions],
            "readings": summarize_readings(regions, size),
        }
    return result


def run_experiment(
    image_path: Path | str, output_dir: Path | str, *,
    recognize: Callable[[SliceRequest], tuple[dict, dict]], model_identity: dict[str, str],
    policy: SlicePolicy = SlicePolicy(), prompt_version: str = PROMPT_VERSION,
    system_prompt: str = SYSTEM_PROMPT, user_prompt: str | None = None,
    instruction_suffix: str = "", fail_fast: bool = True, dry_run: bool = False,
    overview_only: bool = False,
) -> dict:
    """Run only explicitly supplied recognition; no model/client/config discovery.

    Callback returns (raw payload, call metadata). It owns cost limits, model
    logging and retries. This runner writes only output_dir, never any database.
    Use an isolated output directory for each invocation (not concurrent runs).
    """
    if (not isinstance(model_identity, dict) or not model_identity or
            any(not isinstance(key, str) or not isinstance(value, str)
                for key, value in model_identity.items())):
        raise ValueError("explicit nonempty string model identity is required")
    source_path, root = Path(image_path), Path(output_dir)
    source_sha = _image_sha(source_path)
    prompt = build_user_prompt(prompt_version) if user_prompt is None else user_prompt
    identity = {"policy": asdict(policy), "model": model_identity,
                "prompt_version": prompt_version, "system_prompt": system_prompt,
                "user_prompt": prompt, "overview_instruction": OVERVIEW_INSTRUCTION,
                "detail_instruction": DETAIL_INSTRUCTION, "instruction_suffix": instruction_suffix}
    experiment_key = _digest({"source_sha256": source_sha, **identity})
    with Image.open(source_path) as source:
        source.load()
        size = source.size
        regions = plan_slices(*size, policy)
        manifest = {"experiment_key": experiment_key, "identity": identity,
                    "source_image_sha256": source_sha, "image_size": list(size),
                    "status": "partial", "quality_status": "unassessed",
                    "execution_scope": "overview_only" if overview_only else "all_regions",
                    "dry_run": dry_run,
                    "regions": regions, "model_calls": 0}

        def checkpoint():
            completed = sum(row.get("state") == "completed" for row in regions)
            manifest.update(completed_regions=completed, total_regions=len(regions),
                            incomplete_region_ids=[row["id"] for row in regions if row.get("state") != "completed"])
            manifest["status"] = "completed" if completed == len(regions) else "partial"
            manifest["readings"] = summarize_readings(regions, size)
            manifest["views"] = paired_views(manifest)
            _write_json(root / "manifest.json", manifest)

        checkpoint()
        for region in regions:
            if overview_only and region["kind"] != "overview":
                break
            try:
                crop_path = source_path if region["kind"] == "overview" else root / "images" / experiment_key / f"{region['id']}.png"
                if region["kind"] != "overview":
                    crop_path.parent.mkdir(parents=True, exist_ok=True)
                    # Native pixel crop; fixed overview uses the original bytes.
                    source.crop(tuple(region["box"])).save(crop_path)
                crop_sha = _image_sha(crop_path)
                instruction = (OVERVIEW_INSTRUCTION if region["kind"] == "overview" else DETAIL_INSTRUCTION)
                instruction += instruction_suffix
                cache_identity = {"experiment_key": experiment_key, "region_id": region["id"],
                                  "pixel_box": region["box"], "crop_sha256": crop_sha,
                                  "source_image_sha256": source_sha, "image_size": list(size)}
                cache_key = _digest(cache_identity)
                request = SliceRequest(region["id"], region["kind"], crop_path, crop_sha,
                                       source_sha, tuple(region["box"]), size, instruction,
                                       prompt_version, system_prompt, prompt, cache_key)
                region.update(image_path=str(crop_path.resolve()), image_sha256=crop_sha,
                              cache_key=cache_key, instruction=instruction)
                if dry_run:
                    region["state"] = "planned"
                    checkpoint()
                    continue
                cache_path = root / "cache" / f"{cache_key}.json"
                if cache_path.is_file():
                    cache = json.loads(cache_path.read_text(encoding="utf-8"))
                    if cache.get("identity") != cache_identity:
                        raise _CacheIntegrityError("CACHE_IDENTITY_MISMATCH")
                    raw = cache["raw_payload"]
                    if cache.get("raw_payload_sha256") != _digest(raw):
                        raise _CacheIntegrityError("CACHE_PAYLOAD_DIGEST_MISMATCH")
                    payload = _prepare(raw)
                    region["cache_hit"] = True
                    region["call_metadata"] = cache.get("call_metadata", {})
                else:
                    raw, metadata = recognize(request)
                    region.update(raw_payload=_safe_record(raw), call_metadata=_safe_record(metadata))
                    # Count actual callback-reported calls, including validly returned
                    # malformed payloads; exceptions can carry model_calls as well.
                    count = int(metadata.get("model_calls", 1))
                    if count < 0:
                        raise ValueError("negative model_calls")
                    manifest["model_calls"] += count
                    payload = _prepare(raw)
                    _write_json(cache_path, {"identity": cache_identity, "raw_payload": raw,
                                            "raw_payload_sha256": _digest(raw),
                                            "call_metadata": metadata})
                    region["cache_hit"] = False
                    region["call_metadata"] = metadata
                region.update(state="completed", image_sha256=crop_sha, cache_key=cache_key,
                              raw_payload=deepcopy(raw), payload=payload)
            except Exception as exc:
                manifest["model_calls"] += int(getattr(exc, "model_calls", 0) or 0)
                region.update(state="failed", **_safe_error(exc))
                checkpoint()
                if fail_fast:
                    break
            checkpoint()
        checkpoint()
    return manifest
